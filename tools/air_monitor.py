"""Air monitor: watch the 2440 MHz LoRa link over the air with the HackRF Pro, in a window.

It shows, live:
  - the spectrum and waterfall around 2440 MHz (the LoRa channel is shaded),
  - a timeline of the channel power over the last 2 s (every packet is a burst; TDMA slots line up),
  - every burst it hears, decoded with lora_phy: payload text and hex, CRC, level, SNR, frequency error,
    duration and the time since the previous packet.

Radio settings match the v0.1 nodes: 2440.0 MHz, LoRa SF7, BW 1625 kHz, CR 4/5, sync word 0x12.
The HackRF is tuned 1.5 MHz below the channel (2438.5 MHz) so its own DC spike stays out of the LoRa band.

    python3 tools/air_monitor.py                      # live from the HackRF
    python3 tools/air_monitor.py --lna 16 --vga 20    # start with other gains
    python3 tools/air_monitor.py --file CAP.iq        # replay a capture made with the Record button

The HackRF only receives here. Keep it at least 1 m from a node that is transmitting, RF amp off, and close
Gqrx first (only one program can use the HackRF at a time). Packets go to logs/air_<time>.csv.
"""
import argparse
import collections
import csv
import datetime
import multiprocessing as mp
import os
import queue
import subprocess
import sys
import threading
import time

import numpy as np
from scipy import fft as sfft, signal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lora_phy as phy  # noqa: E402  (SF, BW, N, DOWN, wrap, decode_payload)

CH = 2440.0e6              # channel centre
FS = 6.5e6                 # HackRF sample rate = 4 x chip rate
OS = 4
OFFSET = 1.5e6             # HackRF tuned this far below the channel
FC = CH - OFFSET
N = phy.N                  # chips per symbol (128 at SF7)
BW = phy.BW
BLOCK = 325_000            # samples per read (50 ms)
NFFT = 1024
HZ_PER_BIN = BW / N
HIST_BLOCKS = int(2.5 * BW / N)      # power history used for the noise floor and the timeline
MIN_RUN = 8                          # blocks above threshold before a burst counts (a preamble is 12)
MAX_BURST = 600                      # blocks (~47 ms): longer bursts are cut here
THR_DB = 10.0                        # energy threshold above the noise floor (burst start)
END_DB = 4.0                         # a strong burst ends after 4 blocks below this (hysteresis)
PRE_RATIO = 4.5                      # dechirped peak / mean magnitude for a preamble block (noise is ~2.5)
PRE_RUN = 4                          # consecutive preamble blocks to trigger on a weak packet
WEAK_LEN = 120                       # blocks decoded after a weak (below-threshold) preamble (~9.5 ms)
WORKERS = min(4, os.cpu_count() or 1)   # threads for the FFTs
LOGS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")

DOWN = np.array(phy.DOWN, np.complex64)   # dechirps upchirps
UP = np.conj(DOWN)                        # dechirps downchirps


# --------------------------------------------------------------------------------------------- LoRa DSP
def fpeaks(x, starts, ref):
    """Dechirped FFT peak of each window x[s:s+N]: (fractional bin, peak magnitude, peak / mean)."""
    starts = np.asarray(starts, dtype=np.int64)
    m = np.abs(sfft.fft(x[starts[:, None] + np.arange(N)] * ref, axis=1, workers=WORKERS))
    k = m.argmax(axis=1)
    r = np.arange(len(k))
    a, b, c = m[r, (k - 1) % N], m[r, k], m[r, (k + 1) % N]
    den = a - 2 * b + c
    d = np.where(den != 0, 0.5 * (a - c) / np.where(den != 0, den, 1), 0.0)
    return (k + d) % N, b, b / (m.mean(axis=1) + 1e-12)


def wrap(v):
    return (v + N / 2) % N - N / 2


def looks_like_lora(x, start):
    """Four windows from the burst start dechirp to the same, sharp peak (a LoRa preamble)."""
    if start + 4 * N > len(x):
        return False
    pos, _, ratio = fpeaks(x, start + N * np.arange(4), DOWN)
    spread = np.ptp(wrap(pos - pos[0]))
    return spread <= 1.5 and np.min(ratio) > PRE_RATIO


def preamble_snr_db(x, starts):
    """In-channel SNR from dechirped preamble symbols: (peak bin / mean other bin - 1) / N.
    Works below the noise floor, like the SNR the SX1281 reports."""
    starts = np.asarray(starts, dtype=np.int64)
    m2 = np.abs(sfft.fft(x[starts[:, None] + np.arange(N)] * DOWN, axis=1, workers=WORKERS)) ** 2
    k = m2.argmax(axis=1)
    near = (k[:, None] + np.arange(-3, 4)) % N                        # peak and its leakage
    sig_rows = np.take_along_axis(m2, near[:, 2:5], axis=1).sum(axis=1)
    noise = (m2.sum(axis=1) - np.take_along_axis(m2, near, axis=1).sum(axis=1)) / (N - 7)
    snr = np.mean((sig_rows - 3 * noise) / (noise + 1e-20) / N)
    return 10 * np.log10(max(snr, 1e-3))


def decode_symbols(x, start):
    """numpy port of lora_phy.decode_symbols. Returns dict(sync, syms, cfo_hz) or None."""
    if start + N > len(x):
        return None
    p0 = fpeaks(x, [start], DOWN)[0][0]
    k = start
    while True:                                      # walk the preamble until the sync word appears
        if k + N > len(x):
            return None
        b = fpeaks(x, [k], DOWN)[0][0]
        if abs(wrap(b - p0)) > 1.5:
            break
        k += N
        if k > start + 20 * N:
            return None
    sfd = 2 * N + int(2.25 * N)
    if k + sfd + N // 2 + 17 * N > len(x) or k - N // 2 - 7 * N < 0:
        return None

    def energy(ts, nsym=16):
        st = (k + sfd + np.asarray(ts))[:, None] + N * np.arange(nsym)
        return fpeaks(x, st.ravel(), DOWN)[1].reshape(len(ts), nsym).sum(axis=1)

    ts = np.arange(-N // 2, N // 2, 4)
    t = int(ts[np.argmax(energy(ts))])
    ts = np.arange(t - 3, t + 4)
    t = int(ts[np.argmax(energy(ts))])
    ks = k + t                                        # aligned start of the first sync symbol
    pre = fpeaks(x, np.arange(ks - 6 * N, ks - N, N), DOWN)[0]
    c = (pre[0] + np.mean(wrap(pre - pre[0]))) % N

    def syms_at(starts):
        return (np.round(wrap(fpeaks(x, starts, DOWN)[0] - c)).astype(int) % N).tolist()

    sync = tuple(syms_at([ks, ks + N]))
    pay = ks + sfd
    count = min(300, (len(x) - pay) // N)
    syms = syms_at(pay + N * np.arange(count)) if count > 0 else []
    # Frequency error: an upchirp's dechirped peak moves with (CFO - timing), a downchirp's with (CFO + timing).
    down = fpeaks(x, [ks + 2 * N], UP)[0][0]
    cfo_bins = (wrap(c) + wrap(down)) / 2
    return dict(sync=sync, syms=syms, cfo_hz=-cfo_bins * HZ_PER_BIN,
                snr_db=preamble_snr_db(x, np.arange(ks - 6 * N, ks - N, N)))


# --------------------------------------------------------------------------------------------- receiver
class Receiver:
    """Turns raw HackRF int8 IQ (at FS, tuned to fc) into spectrum rows, channel power and decoded packets."""

    def __init__(self, fc=FC):
        self.offset = CH - fc
        self.h = signal.firwin(65, 0.95e6, fs=FS).astype(np.float32)
        self.tail = np.zeros(len(self.h) - 1, np.complex64)    # filter history (overlap-save)
        self.n = 0                                      # input samples so far
        self.mix_base = np.exp(-2j * np.pi * self.offset / FS * np.arange(BLOCK)).astype(np.complex64)
        self.win = np.hanning(NFFT).astype(np.float32)
        self.wnorm = float(self.win.sum()) ** 2
        # Filtered, conjugated samples at FS (4 per chip). Kept at full rate so each packet can be decoded at
        # the best of the 4 sample phases: one sample per chip at the wrong phase corrupts symbols.
        self.arr = np.zeros(int(1.0 * FS), np.complex64)   # storage; live data is arr[self.s:self.e]
        self.s = self.e = 0
        self.buf0 = 0                                   # chip index of buf[0] (buf[0] is sample 4 * buf0)
        self.next_blk = 0                               # chip index of the next power block
        self.hist = np.zeros(0)                         # recent block powers (linear)
        self.run = 0                                    # consecutive blocks above the energy threshold
        self.prun = 0                                   # consecutive blocks that look like a LoRa preamble
        self.ppos = 0.0
        self.burst = None
        self.pending = []
        self.last_lora_chip = None
        self.t0 = None                                  # wall time of chip 0

    def process(self, raw):
        if self.t0 is None:
            self.t0 = time.time() - len(raw) / 2 / FS
        iq = np.frombuffer(raw, np.int8).astype(np.float32).view(np.complex64) / 128.0
        out = {}

        # spectrum: mean and max over this read, 0 dBFS = full-scale tone
        nf = len(iq) // NFFT
        if nf:
            P = np.abs(sfft.fft(iq[:nf * NFFT].reshape(nf, NFFT) * self.win, axis=1, workers=WORKERS)) ** 2 / self.wnorm
            out["spec_mean"] = np.fft.fftshift(10 * np.log10(P.mean(axis=0) + 1e-20))
            out["spec_max"] = np.fft.fftshift(10 * np.log10(P.max(axis=0) + 1e-20))

        # channel: mix down, low-pass (stays at FS), conjugate (see lora_phy.read_iq)
        n0 = self.n
        x = iq
        if self.offset:
            rot = np.exp(-2j * np.pi * ((self.offset / FS * n0) % 1.0))
            base = self.mix_base if len(x) == BLOCK else \
                np.exp(-2j * np.pi * self.offset / FS * np.arange(len(x))).astype(np.complex64)
            x = x * (base * np.complex64(rot))
        xx = np.concatenate([self.tail, x])
        y = signal.oaconvolve(xx, self.h, mode="valid").astype(np.complex64)
        self.tail = xx[len(xx) - len(self.tail):]
        self.n += len(x)
        self._append(np.conj(y))
        end = self.buf0 + len(self.buf) // OS          # chips available

        # power per symbol-length block
        nb = (end - self.next_blk) // N
        p = np.zeros(0)
        if nb > 0:
            i0 = (self.next_blk - self.buf0) * OS
            p = np.mean(np.abs(self.buf[i0:i0 + nb * N * OS].reshape(nb, N * OS)) ** 2, axis=1)
            blk0 = self.next_blk // N
            self.next_blk += nb * N
            self.hist = np.concatenate([self.hist, p])[-HIST_BLOCKS:]
        floor = float(np.percentile(self.hist, 20)) if len(self.hist) else 1e-12
        thr = floor * 10 ** (THR_DB / 10)
        thr_end = floor * 10 ** (END_DB / 10)
        out["floor_db"] = 10 * np.log10(floor + 1e-20)
        out["thr_db"] = out["floor_db"] + THR_DB
        if nb > 0:
            out["blk_t"] = (blk0 + np.arange(nb)) * N / BW          # stream seconds
            out["blk_db"] = 10 * np.log10(p + 1e-20)
            out["now"] = self.next_blk / BW

        # Burst detection, two triggers:
        #  energy:   MIN_RUN blocks THR_DB above the floor (any strong burst, LoRa or not; its length is measured)
        #  preamble: PRE_RUN blocks whose dechirped spectrum has the same sharp peak (LoRa only, works below
        #            the noise floor; a weak packet gets a fixed WEAK_LEN window because its end can't be seen)
        if nb > 0:
            i0 = (blk0 * N - self.buf0) * OS
            pos, _, ratio = fpeaks(self.buf[i0:i0 + nb * N * OS:OS], N * np.arange(nb), DOWN)
        if nb > 0 and self.burst is None and self.run == 0 and self.prun == 0 \
                and not (p > thr).any() and not (ratio > PRE_RATIO).any():
            p = []                                        # idle channel: nothing to track
        else:
            p, pos, ratio = list(p), list(pos) if nb > 0 else [], list(ratio) if nb > 0 else []
        for i, pi in enumerate(p):
            b = blk0 + i
            strong = pi > thr
            self.run = self.run + 1 if strong else 0
            held = pi > thr_end
            if ratio[i] > PRE_RATIO and (self.prun == 0 or abs(wrap(pos[i] - self.ppos)) <= 1.5):
                self.prun += 1
            else:
                self.prun = 1 if ratio[i] > PRE_RATIO else 0
            self.ppos = pos[i]
            if self.burst is None:
                if self.run >= MIN_RUN:
                    self.burst = dict(start=b - self.run + 1, below=0, strong=True, pre=False)
                elif self.prun >= PRE_RUN:
                    self.burst = dict(start=b - self.prun + 1, below=0, strong=False, pre=True)
                continue
            bu = self.burst
            if self.prun >= PRE_RUN:
                bu["pre"] = True
            if self.run >= MIN_RUN:
                bu["strong"] = True
            bu["below"] = 0 if held else bu["below"] + 1
            length = b - bu["start"]
            if (bu["strong"] and bu["below"] >= 4) or length >= MAX_BURST or \
                    (not bu["strong"] and length >= WEAK_LEN):
                bu["end"] = b - bu["below"] + 1 if bu["strong"] else b + 1
                self.pending.append(bu)
                self.burst = None
                self.run = self.prun = 0

        # decode bursts once enough samples follow them
        packets, keep = [], []
        for bu in self.pending:
            s_chip = (bu["start"] - 4) * N
            e_chip = (max(bu["end"], bu["start"] + WEAK_LEN) + 24) * N      # room for the longest packet
            if e_chip > end:
                keep.append(bu)
                continue
            if s_chip < self.buf0:
                continue
            packets.append(self._packet(bu, s_chip, e_chip, floor))
        self.pending = keep
        out["packets"] = packets

        # keep 0.25 s of samples, more if a burst is still open or pending
        lo = end - int(0.25 * BW)
        for bu in self.pending + ([self.burst] if self.burst else []):
            lo = min(lo, (bu["start"] - 8) * N)
        lo = max(lo, self.buf0)
        self.s += (lo - self.buf0) * OS
        self.buf0 = lo
        return out

    def gap(self, nsamp):
        """nsamp input samples were lost: restart the stream state after them, keep the time base."""
        self.n += nsamp
        self.tail[:] = 0
        self.s = self.e = 0
        self.buf0 = self.n // OS
        self.next_blk = -(-self.buf0 // N) * N
        self.burst, self.pending, self.run, self.prun = None, [], 0, 0

    @property
    def buf(self):
        return self.arr[self.s:self.e]

    def _append(self, v):
        if self.e + len(v) > len(self.arr):                  # out of room: move the live data to the front
            live = self.e - self.s
            if live + len(v) > len(self.arr):
                self.arr = np.concatenate([self.arr, np.zeros(live + len(v), np.complex64)])
            self.arr[:live] = self.arr[self.s:self.e]
            self.s, self.e = 0, live
        self.arr[self.e:self.e + len(v)] = v
        self.e += len(v)

    def _packet(self, bu, s_chip, e_chip, floor):
        i0, i1 = (s_chip - self.buf0) * OS, (e_chip - self.buf0) * OS
        segs = [self.buf[i0 + ph:i1 + ph:OS] for ph in range(OS)]       # the 4 sample phases, at chip rate
        start = 5 * N                                    # one block into the burst, like lora_phy.find_packets
        body = self.buf[(bu["start"] * N - self.buf0) * OS:(bu["end"] * N - self.buf0) * OS]
        level = float(np.mean(np.abs(body) ** 2)) if len(body) else floor
        chip0 = bu["start"] * N
        pk = dict(chip=chip0, stream_s=chip0 / BW, wall=self.t0 + chip0 / BW,
                  dur_ms=(bu["end"] - bu["start"]) * N / BW * 1e3 if bu["strong"] else None,
                  level_db=10 * np.log10(level + 1e-20), snr_db=10 * np.log10(level / floor + 1e-20),
                  kind="other", length=None, header_ok=None, crc_ok=None, data=b"", sync=None,
                  cfo_khz=None, dt_ms=None)
        if not (bu["pre"] or any(looks_like_lora(sg, start) for sg in segs)):
            return pk                                    # no LoRa preamble seen: Wi-Fi, Bluetooth, noise
        pk["kind"] = "bad"
        if self.last_lora_chip is not None:
            pk["dt_ms"] = (chip0 - self.last_lora_chip) / BW * 1e3
        self.last_lora_chip = chip0
        # try the sample phases, sharpest preamble first; keep the first that passes the CRC
        sharp = [float(np.sum(fpeaks(s, start + N * np.arange(4), DOWN)[2])) for s in segs]
        first = True
        for ph in np.argsort(sharp)[::-1]:
            try:
                r = decode_symbols(segs[ph], start)
                if r is None:
                    continue
                length, hdr_ok, data, crc_ok = phy.decode_payload(r["syms"])
            except Exception:                             # noqa: BLE001  (garbage symbols)
                continue
            if first or (hdr_ok and crc_ok):
                pk.update(sync=r["sync"], cfo_khz=r["cfo_hz"] / 1e3, length=length, header_ok=hdr_ok,
                          data=data, crc_ok=crc_ok, snr_db=r["snr_db"])
                first = False
            if hdr_ok and crc_ok:
                pk["kind"] = "ok"
                break
        return pk


# --------------------------------------------------------------------------------------------- sources
def _read_exact(src, n):
    chunks, got = [], 0
    while got < n:
        c = src.read(n - got)
        if not c:
            return None
        chunks.append(c)
        got += len(c)
    return b"".join(chunks)


def _stop_requested(ctl_q):
    try:
        while True:
            if ctl_q.get_nowait()[0] == "stop":
                return True
    except queue.Empty:
        return False


def dsp_main(out_q, ctl_q, lna, vga, file, file_fc, realtime):
    """Runs in its own process, so the window never slows the receiver down.
    A reader thread pulls IQ from hackrf_transfer (or a file) into a 2 s buffer and records it if asked; the
    main loop runs the Receiver. If the buffer ever overflows, whole blocks are dropped, counted and shown,
    and the Receiver is told about the gap so packet timing stays right."""
    blocks = queue.Queue(maxsize=40)
    stop = threading.Event()
    rec_lock = threading.Lock()
    st = dict(read=0, dropped=0, t0=None, rec=None)
    err_lines = collections.deque(maxlen=5)
    proc = None
    try:
        if file:
            src = open(file, "rb")
            out_q.put(("status", f"Replaying {os.path.basename(file)}"))
        else:
            cmd = ["hackrf_transfer", "-r", "-", "-f", str(int(FC)), "-s", str(int(FS)),
                   "-a", "0", "-l", str(lna), "-g", str(vga)]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            try:                                          # 1 MB pipe = 80 ms of slack instead of 5 ms
                import fcntl
                fcntl.fcntl(proc.stdout.fileno(), 1031, 1 << 20)       # F_SETPIPE_SZ
            except OSError:
                pass

            def drain():
                for line in iter(proc.stderr.readline, b""):
                    err_lines.append(line.decode(errors="replace").strip())
            threading.Thread(target=drain, daemon=True).start()
            src = proc.stdout
            out_q.put(("status", f"HackRF receiving: {FC / 1e6:.1f} MHz, {FS / 1e6:.1f} MS/s, "
                                 f"LNA {lna} dB, VGA {vga} dB, amp off"))

        def reader():
            idx = 0
            while not stop.is_set():
                raw = _read_exact(src, 2 * BLOCK)
                if raw is None:
                    blocks.put(None)
                    return
                if st["t0"] is None:
                    st["t0"] = time.time()
                with rec_lock:
                    if st["rec"]:
                        st["rec"].write(raw)
                try:
                    blocks.put((idx, raw), timeout=None if file else 0)
                except queue.Full:
                    st["dropped"] += 1
                idx += BLOCK
                st["read"] = idx
                if file and realtime:
                    lag = idx / FS - (time.time() - st["t0"])
                    if lag > 0:
                        time.sleep(lag)
        threading.Thread(target=reader, daemon=True).start()

        rx = Receiver(fc=file_fc if file else FC)
        expected = 0
        while True:
            try:
                while True:
                    cmd_, arg = ctl_q.get_nowait()
                    if cmd_ == "stop":
                        return
                    if cmd_ == "record":
                        with rec_lock:
                            if st["rec"]:
                                st["rec"].close()
                            st["rec"] = open(arg, "wb") if arg else None
            except queue.Empty:
                pass
            try:
                item = blocks.get(timeout=0.5)
            except queue.Empty:
                if proc and proc.poll() is not None:
                    time.sleep(0.2)
                    out_q.put(("error", " | ".join(l for l in err_lines if l) or "hackrf_transfer stopped"))
                    return
                continue
            if item is None:
                if file:
                    out_q.put(("status", "Replay finished"))
                else:
                    time.sleep(0.2)
                    out_q.put(("error", " | ".join(l for l in err_lines if l) or "hackrf_transfer stopped"))
                return
            idx, raw = item
            if idx > expected:
                rx.gap(idx - expected)
            expected = idx + BLOCK
            out = rx.process(raw)
            el = time.time() - st["t0"] if st["t0"] else 0
            out["rate"] = st["read"] / el if el > 1 else FS
            out["dropped"] = st["dropped"]
            out["backlog"] = blocks.qsize()
            while True:                                   # wait for room, but still obey "stop"
                try:
                    out_q.put(("data", out), timeout=0.2)
                    break
                except queue.Full:
                    if _stop_requested(ctl_q):
                        return
    except Exception as e:                                # noqa: BLE001
        out_q.put(("error", f"{type(e).__name__}: {e}"))
    finally:
        stop.set()
        if proc and proc.poll() is None:
            proc.send_signal(2)                           # SIGINT: hackrf_transfer stops the HackRF cleanly
            try:
                proc.stdout.close()                       # unblocks it if it is stuck writing to the pipe
            except OSError:
                pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        with rec_lock:
            if st["rec"]:
                st["rec"].close()


class Worker:
    """Starts and stops the DSP process. Results arrive on the queue passed in."""

    def __init__(self, q, lna, vga, file=None, file_fc=FC, realtime=True):
        self.q = q
        self.ctl = mp.Queue()
        self.p = mp.Process(target=dsp_main, args=(q, self.ctl, lna, vga, file, file_fc, realtime), daemon=True)

    def start(self):
        self.p.start()

    def set_record(self, path):
        self.ctl.put(("record", path))

    def stop(self):
        """Ask the process to stop and keep emptying its result queue until it has gone (results are dropped).
        Killing it while it writes to the queue would leave half a message there and block the reader."""
        if not self.p.is_alive():
            return
        self.ctl.put(("stop", None))
        t = time.time()
        while self.p.is_alive() and time.time() - t < 5:
            try:
                while True:
                    self.q.get_nowait()
            except (queue.Empty, OSError, EOFError):
                pass
            self.p.join(0.05)
        if self.p.is_alive():
            self.p.terminate()
            self.p.join(2)


# --------------------------------------------------------------------------------------------- GUI
def run_gui(args):
    import pyqtgraph as pg
    from pyqtgraph.Qt import QtCore, QtGui, QtWidgets

    pg.setConfigOptions(antialias=False)
    KIND = {"ok": ("LoRa OK", "#2e9d4f"), "bad": ("LoRa error", "#d9534f"), "other": ("Other burst", "#8a8a8a")}
    LNA_STEPS = [0, 8, 16, 24, 32, 40]

    class Tile(QtWidgets.QFrame):
        def __init__(self, title):
            super().__init__()
            self.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
            lay = QtWidgets.QVBoxLayout(self)
            lay.setContentsMargins(8, 4, 8, 4)
            t = QtWidgets.QLabel(title)
            t.setStyleSheet("color: palette(mid); font-size: 11px;")
            self.v = QtWidgets.QLabel("–")
            f = self.v.font()
            f.setPointSize(f.pointSize() + 7)
            f.setBold(True)
            self.v.setFont(f)
            lay.addWidget(t)
            lay.addWidget(self.v)

        def set(self, text, color=None):
            self.v.setText(text)
            self.v.setStyleSheet(f"color: {color};" if color else "")

    class Main(QtWidgets.QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("Air Monitor: 2440 MHz LoRa (HackRF Pro)")
            self.resize(1300, 950)
            self.q = mp.Queue(maxsize=400)
            self.worker = None
            self.csv = None
            self.reset_stats()

            central = QtWidgets.QWidget()
            self.setCentralWidget(central)
            root = QtWidgets.QVBoxLayout(central)

            # controls
            bar = QtWidgets.QHBoxLayout()
            self.btn = QtWidgets.QPushButton("Start")
            self.btn.setMinimumWidth(90)
            self.btn.clicked.connect(self.toggle)
            self.lna = QtWidgets.QComboBox()
            self.lna.addItems([str(v) for v in LNA_STEPS])
            self.lna.setCurrentText(str(args.lna))
            self.vga = QtWidgets.QSpinBox()
            self.vga.setRange(0, 62)
            self.vga.setSingleStep(2)
            self.vga.setValue(args.vga)
            self.lna.currentTextChanged.connect(self.gain_changed)
            self.vga.valueChanged.connect(self.gain_changed)
            self.rec = QtWidgets.QCheckBox("Record IQ (13 MB/s)")
            self.rec.toggled.connect(self.record_toggled)
            clear = QtWidgets.QPushButton("Clear")
            clear.clicked.connect(self.clear)
            self.status = QtWidgets.QLabel("Stopped")
            self.health = QtWidgets.QLabel("")
            self.resize_due = False
            for w in (self.btn, QtWidgets.QLabel("LNA dB"), self.lna, QtWidgets.QLabel("VGA dB"), self.vga,
                      self.rec, clear):
                bar.addWidget(w)
            bar.addWidget(self.status, 1)
            bar.addWidget(self.health)
            if args.file:
                for w in (self.lna, self.vga, self.rec):
                    w.setEnabled(False)
            root.addLayout(bar)

            # tiles
            grid = QtWidgets.QGridLayout()
            names = ["LoRa packets OK", "LoRa errors", "Other bursts", "OK packets / s (5 s)",
                     "Interval (last)", "Level", "SNR", "Freq error"]
            self.tiles = {}
            for i, n in enumerate(names):
                self.tiles[n] = Tile(n)
                grid.addWidget(self.tiles[n], 0, i)
            root.addLayout(grid)

            split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
            root.addWidget(split, 1)
            f_mhz = (FC + (np.arange(NFFT) - NFFT // 2) * FS / NFFT) / 1e6
            self.f_mhz = f_mhz
            lo_ch, hi_ch = (CH - BW / 2) / 1e6, (CH + BW / 2) / 1e6

            # spectrum
            self.spec = pg.PlotWidget(title="Spectrum (dBFS): live and peak hold")
            self.spec.setLabel("bottom", "MHz")
            self.spec.showGrid(x=True, y=True, alpha=0.25)
            self.spec.setYRange(-110, -10)
            self.spec.setXRange(f_mhz[0], f_mhz[-1], padding=0)
            self.spec.addItem(pg.LinearRegionItem((lo_ch, hi_ch), movable=False,
                                                  brush=pg.mkBrush(46, 157, 79, 40)))
            self.c_max = self.spec.plot(pen=pg.mkPen("#e0a030", width=1))
            self.c_mean = self.spec.plot(pen=pg.mkPen("#3a8fd8", width=1))
            self.peak = None
            split.addWidget(self.spec)

            # waterfall
            self.wf_rows = 200
            self.wf = np.full((self.wf_rows, NFFT), -110.0, np.float32)
            wfw = pg.PlotWidget(title="Waterfall (peak per 50 ms, newest at top, 10 s)")
            wfw.setLabel("bottom", "MHz")
            wfw.hideAxis("left")
            self.img = pg.ImageItem()
            self.img.setColorMap(pg.colormap.get("viridis"))
            self.wf_rect = QtCore.QRectF(f_mhz[0], 0, f_mhz[-1] - f_mhz[0], self.wf_rows)
            wfw.addItem(self.img)
            wfw.setXLink(self.spec)
            split.addWidget(wfw)

            # timeline
            self.tl = pg.PlotWidget(title="Channel power, last 2 s (each packet is a burst; "
                                          "green = decoded, red = LoRa error, grey = other)")
            self.tl.setLabel("bottom", "seconds before now")
            self.tl.setLabel("left", "dBFS")
            self.tl.showGrid(x=True, y=True, alpha=0.25)
            self.tl.setXRange(-2, 0)
            self.tl.setYRange(-90, -10)
            self.c_tl = self.tl.plot(pen=pg.mkPen("#3a8fd8", width=1))
            self.thr_line = pg.InfiniteLine(angle=0, pen=pg.mkPen("#e0a030", style=QtCore.Qt.PenStyle.DashLine))
            self.tl.addItem(self.thr_line)
            self.marks = pg.ScatterPlotItem(size=9, pen=None)
            self.tl.addItem(self.marks)
            self.tl_t = collections.deque()
            self.tl_db = collections.deque()
            self.now = 0.0
            split.addWidget(self.tl)

            # packet table
            cols = ["Time", "Type", "Δt ms", "Len", "CRC", "Level dBFS", "SNR dB", "Freq err kHz",
                    "Dur ms", "Text", "Hex"]
            self.table = QtWidgets.QTableWidget(0, len(cols))
            self.table.setHorizontalHeaderLabels(cols)
            self.table.verticalHeader().setVisible(False)
            self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
            self.table.horizontalHeader().setStretchLastSection(True)
            mono = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont)
            self.table.setFont(mono)
            split.addWidget(self.table)
            split.setSizes([200, 220, 200, 330])

            self.timer = QtCore.QTimer(self)
            self.timer.timeout.connect(self.poll)
            self.timer.start(50)
            if args.file or args.autostart:
                QtCore.QTimer.singleShot(200, self.toggle)

        # ----- state
        def reset_stats(self):
            self.count = {"ok": 0, "bad": 0, "other": 0}
            self.ok_times = collections.deque()

        def clear(self):
            self.reset_stats()
            self.table.setRowCount(0)
            self.peak = None
            self.marks.clear()
            self.mark_pts = []
            for t in self.tiles.values():
                t.set("–")

        def toggle(self):
            if self.worker:
                self.stop()
            else:
                self.start()

        def start(self):
            self.rec.setChecked(False)
            self.q = mp.Queue(maxsize=400)                # fresh queue per run
            self.worker = Worker(self.q, int(self.lna.currentText()), self.vga.value(), file=args.file,
                                 file_fc=args.file_fc * 1e6, realtime=not args.fast)
            self.worker.start()
            self.btn.setText("Stop")
            self.mark_pts = []
            self.tl_t.clear()
            self.tl_db.clear()
            if self.csv is None:
                os.makedirs(LOGS, exist_ok=True)
                path = os.path.join(LOGS, f"air_{datetime.datetime.now():%Y%m%d_%H%M%S}.csv")
                self.csv_f = open(path, "w", newline="", buffering=1)
                self.csv = csv.writer(self.csv_f)
                self.csv.writerow(["time", "stream_s", "type", "dt_ms", "dur_ms", "level_dbfs", "snr_db",
                                   "freq_err_khz", "len", "header_ok", "crc_ok", "sync", "payload_hex", "text"])
                self.csv_path = path

        def stop(self):
            if self.worker:
                self.worker.stop()
                self.worker = None
            self.btn.setText("Start")
            self.status.setText("Stopped")

        def gain_changed(self, *_):
            if self.worker and not args.file:
                self.stop()
                self.start()

        def record_toggled(self, on):
            if not self.worker:
                if on:
                    self.rec.setChecked(False)
                return
            if on:
                os.makedirs(LOGS, exist_ok=True)
                path = os.path.join(LOGS, f"capture_{datetime.datetime.now():%Y%m%d_%H%M%S}"
                                          f"_{FC / 1e6:.1f}MHz_{FS / 1e6:.1f}Msps.iq")
                self.worker.set_record(path)
                self.status.setText(f"Recording to {path}")
            else:
                self.worker.set_record(None)

        def closeEvent(self, e):
            self.stop()
            super().closeEvent(e)

        # ----- updates
        def poll(self):
            last = None
            n = 0
            while n < 100:
                try:
                    kind, val = self.q.get_nowait()
                except queue.Empty:
                    break
                n += 1
                if kind == "data":
                    self.take(val)
                    last = val
                elif kind == "status":
                    self.status.setText(val)
                elif kind == "error":
                    self.stop()
                    self.status.setText("HackRF error: " + val)
                    self.status.setStyleSheet("color: #d9534f;")
            if last is None:
                return
            if "spec_mean" in last:
                self.c_mean.setData(self.f_mhz, last["spec_mean"])
                self.c_max.setData(self.f_mhz, self.peak)
                lo = float(np.median(last["spec_mean"]))
                self.img.setImage(self.wf[::-1].T, autoLevels=False, levels=(lo - 3, lo + 45))
                self.img.setRect(self.wf_rect)            # after setImage: the rect scales the image's pixels
            if self.tl_t:
                t = np.fromiter(self.tl_t, float) - self.now
                d = np.fromiter(self.tl_db, float)
                m = len(t) // 4 * 4
                if m:
                    self.c_tl.setData(t[:m].reshape(-1, 4)[:, 0], d[:m].reshape(-1, 4).max(axis=1))
                self.thr_line.setValue(last["thr_db"])
                self.mark_pts = [p for p in self.mark_pts if p[0] > self.now - 2.2]
                self.marks.setData([p[0] - self.now for p in self.mark_pts], [p[1] for p in self.mark_pts],
                                   brush=[pg.mkBrush(KIND[p[2]][1]) for p in self.mark_pts])
            self.update_tiles()
            if self.resize_due and time.time() - getattr(self, "last_resize", 0) > 1:
                self.table.resizeColumnsToContents()
                self.resize_due, self.last_resize = False, time.time()

        def take(self, out):
            if "spec_mean" in out:
                self.wf = np.roll(self.wf, 1, axis=0)
                self.wf[0] = out["spec_max"]
                self.peak = out["spec_max"] if self.peak is None else np.maximum(self.peak, out["spec_max"])
            if "blk_t" in out:
                self.tl_t.extend(out["blk_t"])
                self.tl_db.extend(out["blk_db"])
                self.now = out["now"]
                while self.tl_t and self.tl_t[0] < self.now - 2.0:
                    self.tl_t.popleft()
                    self.tl_db.popleft()
            for pk in out["packets"]:
                self.add_packet(pk)
            if "rate" in out:
                bad = out["dropped"] > 0 or out["rate"] < FS * 0.995
                self.health.setText(f"in {out['rate'] / 1e6:.3f} MS/s | dropped blocks {out['dropped']} | "
                                    f"backlog {out['backlog'] * 50} ms")
                self.health.setStyleSheet("color: #d9534f;" if bad else "")

        def add_packet(self, pk):
            self.count[pk["kind"]] += 1
            if pk["kind"] == "ok":
                self.ok_times.append(pk["stream_s"])
            self.mark_pts.append((pk["stream_s"], pk["level_db"], pk["kind"]))
            if pk["kind"] != "other":
                self.last_pk = pk
            text = "".join(chr(b) if 32 <= b < 127 else "." for b in pk["data"])
            hexs = pk["data"].hex(" ")
            fmt = lambda v, f: "" if v is None else format(v, f)                       # noqa: E731
            crc = "" if pk["crc_ok"] is None else ("ok" if pk["crc_ok"] and pk["header_ok"] else "BAD")
            row = [datetime.datetime.fromtimestamp(pk["wall"]).strftime("%H:%M:%S.%f")[:-3],
                   KIND[pk["kind"]][0], fmt(pk["dt_ms"], ".1f"), fmt(pk["length"], "d"), crc,
                   f"{pk['level_db']:.1f}", f"{pk['snr_db']:.1f}", fmt(pk["cfo_khz"], "+.1f"),
                   fmt(pk["dur_ms"], ".2f"), text, hexs]
            if pk["kind"] != "other" or args.show_other:
                self.table.insertRow(0)
                for i, v in enumerate(row):
                    it = QtWidgets.QTableWidgetItem(v)
                    if i == 1:
                        it.setForeground(QtGui.QColor(KIND[pk["kind"]][1]))
                    self.table.setItem(0, i, it)
                if self.table.rowCount() > 500:
                    self.table.removeRow(500)
                self.resize_due = True
            if self.csv:
                self.csv.writerow([datetime.datetime.fromtimestamp(pk["wall"]).isoformat(timespec="milliseconds"),
                                   f"{pk['stream_s']:.6f}", pk["kind"], fmt(pk["dt_ms"], ".3f"),
                                   fmt(pk["dur_ms"], ".3f"), f"{pk['level_db']:.1f}", f"{pk['snr_db']:.1f}",
                                   fmt(pk["cfo_khz"], ".2f"), fmt(pk["length"], "d"), pk["header_ok"],
                                   pk["crc_ok"], pk["sync"], pk["data"].hex(), text])

        def update_tiles(self):
            T = self.tiles
            T["LoRa packets OK"].set(str(self.count["ok"]), KIND["ok"][1] if self.count["ok"] else None)
            T["LoRa errors"].set(str(self.count["bad"]), KIND["bad"][1] if self.count["bad"] else None)
            T["Other bursts"].set(str(self.count["other"]))
            while self.ok_times and self.ok_times[0] < self.now - 5:
                self.ok_times.popleft()
            T["OK packets / s (5 s)"].set(f"{len(self.ok_times) / 5:.1f}")
            pk = getattr(self, "last_pk", None)
            if pk:
                T["Interval (last)"].set("–" if pk["dt_ms"] is None else f"{pk['dt_ms']:.1f} ms")
                T["Level"].set(f"{pk['level_db']:.1f} dBFS",
                               "#d9534f" if pk["level_db"] > -12 else None)   # near clipping: lower the gain
                T["SNR"].set(f"{pk['snr_db']:.1f} dB")
                T["Freq error"].set("–" if pk["cfo_khz"] is None else f"{pk['cfo_khz']:+.1f} kHz")

    app = QtWidgets.QApplication(sys.argv)
    w = Main()
    w.show()
    if args.screenshot:                                   # for testing: save the window after N seconds, quit
        def snap():
            w.grab().save(args.screenshot)
            w.stop()
            app.quit()
        QtCore.QTimer.singleShot(int(args.screenshot_after * 1000), snap)
    sys.exit(app.exec())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lna", type=int, default=16, choices=[0, 8, 16, 24, 32, 40], help="HackRF LNA gain dB")
    ap.add_argument("--vga", type=int, default=20, help="HackRF VGA gain dB (0-62, even)")
    ap.add_argument("--file", help="replay an int8 IQ capture (6.5 MS/s) instead of the HackRF")
    ap.add_argument("--file-fc", type=float, default=FC / 1e6, help="centre frequency of --file in MHz")
    ap.add_argument("--fast", action="store_true", help="replay --file as fast as possible")
    ap.add_argument("--show-other", action="store_true", help="also list non-LoRa bursts (Wi-Fi etc.) in the table")
    ap.add_argument("--autostart", action="store_true", help="start receiving at once")
    ap.add_argument("--screenshot", help=argparse.SUPPRESS)
    ap.add_argument("--screenshot-after", type=float, default=5, help=argparse.SUPPRESS)
    args = ap.parse_args()
    run_gui(args)


if __name__ == "__main__":
    main()
