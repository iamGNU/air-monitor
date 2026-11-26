"""Air monitor: watch the 2440 MHz LoRa link over the air with the HackRF Pro, in a window.

It shows, live:
  - whether the link is working, in plain words, and what to do when it is not,
  - the waterfall around 2440 MHz (the LoRa channel is marked, and every packet heard),
  - a timeline of the channel power over the last 2 s (every packet is a burst; TDMA slots line up),
  - every burst it hears, decoded with lora_phy: payload text, and under "Show details" hex, CRC, level, SNR,
    frequency error, duration and the time since the previous packet, plus the spectrum and raw gains.

Radio settings match the v0.1 nodes: 2440.0 MHz, LoRa SF7, BW 1625 kHz, CR 4/5, sync word 0x12.
The HackRF is tuned 1.5 MHz below the channel (2438.5 MHz) so its own DC spike stays out of the LoRa band.

    python3 tools/air_monitor.py                      # live from the HackRF
    python3 tools/air_monitor.py --demo               # an example recording, no HackRF needed
    python3 tools/air_monitor.py --lna 16 --vga 20    # start with other gains
    python3 tools/air_monitor.py --file CAP.iq        # replay a capture made with "Record raw radio data"

The HackRF only receives here. Keep it at least 1 m from a node that is transmitting, RF amp off, and close
Gqrx first (only one program can use the HackRF at a time). Packets go to logs/air_<time>.csv.
This file is the receiver (DSP); the window is air_monitor_ui.py.
"""
import argparse
import collections
import io
import multiprocessing as mp
import os
import queue
import signal as os_signal
import subprocess
import sys
import threading
import time

import numpy as np
from scipy import fft as sfft, signal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lora_phy as phy  # noqa: E402  (SF, BW, N, DOWN, wrap, decode_payload)
import radio_config  # noqa: E402

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
WEAK_LEN = 120                       # blocks a weak (below-threshold) burst counts as busy (~9.5 ms)
WORKERS = min(4, os.cpu_count() or 1)   # threads for the FFTs
LOGS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")

DOWN = np.array(phy.DOWN, np.complex64)   # dechirps upchirps
UP = np.conj(DOWN)                        # dechirps downchirps
CUTOFF = 0.95e6                           # channel filter cut-off
RADIO = radio_config.DEFAULT              # the settings above, as radio_config.Radio


def _packet_lengths():
    """(payload symbols of the longest LoRa packet, 255 bytes; blocks to keep after a weak preamble to decode it).
    A weak packet's end can't be seen, so it is decoded from a window that fits the longest one."""
    max_symbols = len(phy.encode_symbols(bytes(255)))
    return max_symbols, int(phy.PREAMBLE + 4.25) + 1 + max_symbols + 4


MAX_SYMBOLS, WEAK_SPAN = _packet_lengths()   # 378 and 399 at SF7, CR 4/5


def configure(radio):
    """Use other radio settings (radio_config.Radio). The defaults are the values above. Call before making a
    Receiver; every process calls it (the window, and the receiver process via Worker)."""
    global RADIO, CH, FS, OS, OFFSET, FC, N, BW, BLOCK, HZ_PER_BIN, HIST_BLOCKS, DOWN, UP, CUTOFF, \
        MAX_SYMBOLS, WEAK_SPAN
    phy.configure(radio.sf, radio.bw, radio.cr - 4)
    RADIO, CH, BW, N = radio, radio.freq_hz, phy.BW, phy.N
    OS, FS, OFFSET, CUTOFF, BLOCK = radio.os, radio.fs, radio.offset, radio.cutoff, radio.block
    FC = CH - OFFSET
    HZ_PER_BIN = BW / N
    HIST_BLOCKS = int(2.5 * BW / N)
    DOWN = np.array(phy.DOWN, np.complex64)
    UP = np.conj(DOWN)
    MAX_SYMBOLS, WEAK_SPAN = _packet_lengths()


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
    count = min(MAX_SYMBOLS + 8, (len(x) - pay) // N)                  # up to the longest packet
    syms = syms_at(pay + N * np.arange(count)) if count > 0 else []
    # Frequency error: an upchirp's dechirped peak moves with (CFO - timing), a downchirp's with (CFO + timing).
    down = fpeaks(x, [ks + 2 * N], UP)[0][0]
    cfo_bins = (wrap(c) + wrap(down)) / 2
    return dict(sync=sync, syms=syms, cfo_hz=-cfo_bins * HZ_PER_BIN,
                snr_db=preamble_snr_db(x, np.arange(ks - 6 * N, ks - N, N)))


# --------------------------------------------------------------------------------------------- receiver
class Receiver:
    """Turns raw HackRF int8 IQ (at FS, tuned to fc) into spectrum rows, channel power and decoded packets."""

    def __init__(self, fc=None):
        self.offset = CH - (FC if fc is None else fc)          # (FC read now: configure() may have changed it)
        self.h = signal.firwin(65, CUTOFF, fs=FS).astype(np.float32)
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
            # room for the packet: a strong burst's end is measured; a weak one gets room for the longest packet
            e_chip = (max(bu["end"], bu["start"] + (WEAK_LEN if bu["strong"] else WEAK_SPAN)) + 24) * N
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


def hackrf_command(lna, vga, target="-", extra=()):
    """hackrf_transfer receiving at the current settings into `target` ("-" = stdout). RF amp off."""
    return ["hackrf_transfer", "-r", target, "-f", str(int(FC)), "-s", str(int(FS)), "-a", "0",
            "-l", str(lna), "-g", str(vga), *extra]


def open_hackrf(lna, vga, extra=()):
    """Start hackrf_transfer. Returns (proc, src): src is a binary file object to read the int8 IQ from; read
    proc.stderr for its messages. Linux/macOS: its stdout. Windows: a named pipe, because there hackrf_transfer
    writes stdout in text mode, which would corrupt the samples (every 0x0A byte becomes 0x0D 0x0A)."""
    if os.name == "nt":
        return _open_hackrf_windows(lna, vga, extra)
    proc = subprocess.Popen(hackrf_command(lna, vga, "-", extra), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            bufsize=0)
    try:                                          # Linux: 1 MB pipe = 80 ms of slack instead of 5 ms
        import fcntl
        fcntl.fcntl(proc.stdout.fileno(), 1031, 1 << 20)       # F_SETPIPE_SZ
    except (ImportError, OSError):                # no fcntl (Windows), or not Linux (macOS)
        pass
    return proc, proc.stdout


def _open_hackrf_windows(lna, vga, extra):
    """Windows: hackrf_transfer writes into a named pipe it opens in binary mode ("wb"). We create the pipe
    (with ctypes: no extra packages), start it, and wait for it to connect. If it gives up before that (no HackRF,
    busy...), src is empty and its error is on stderr."""
    import ctypes
    import msvcrt
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateNamedPipeW.restype = wintypes.HANDLE
    k32.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR] + [wintypes.DWORD] * 6 + [wintypes.LPVOID]
    k32.ConnectNamedPipe.restype = wintypes.BOOL
    k32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    invalid = wintypes.HANDLE(-1).value
    name = rf"\\.\pipe\air-monitor-{os.getpid()}-{time.monotonic_ns()}"
    pipe = k32.CreateNamedPipeW(name, 0x1, 0, 1, 0, 1 << 20, 0, None)     # inbound, byte mode, 1 MB buffer
    if pipe in (None, invalid):
        raise ctypes.WinError(ctypes.get_last_error())
    proc = subprocess.Popen(hackrf_command(lna, vga, name, extra), stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
    connected = threading.Event()

    def connect():                                # returns when hackrf_transfer opens the pipe (or we do)
        k32.ConnectNamedPipe(pipe, None)
        connected.set()
    threading.Thread(target=connect, daemon=True).start()
    while not connected.wait(0.05):
        if proc.poll() is not None:               # it exited without opening the pipe: end the wait ourselves
            h = k32.CreateFileW(name, 0x40000000, 0, None, 3, 0, None)   # GENERIC_WRITE, OPEN_EXISTING
            if h not in (None, invalid):
                k32.CloseHandle(h)
            connected.wait(1)
            k32.CloseHandle(pipe)
            return proc, io.BytesIO()
    return proc, os.fdopen(msvcrt.open_osfhandle(pipe, os.O_RDONLY), "rb", buffering=0)


def stop_hackrf(proc, src):
    """Stop hackrf_transfer so that it releases the HackRF cleanly. Linux/macOS: SIGINT. Windows (no SIGINT for
    a child process): closing our end of the pipe makes its next write fail, which ends its main loop the same
    way. Either way, kill it if it is still there after 3 s."""
    if proc.poll() is None and os.name != "nt":
        proc.send_signal(os_signal.SIGINT)
    try:
        src.close()                               # also unblocks it if it is stuck writing to the pipe
    except OSError:
        pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _stop_requested(ctl_q):
    try:
        while True:
            if ctl_q.get_nowait()[0] == "stop":
                return True
    except queue.Empty:
        return False


def dsp_main(out_q, ctl_q, lna, vga, file, file_fc, realtime, loop=False, demo=False, radio=None):
    """Runs in its own process, so the window never slows the receiver down.
    A reader thread pulls IQ from hackrf_transfer (or a file) into a 2 s buffer and records it if asked; the
    main loop runs the Receiver. If the buffer ever overflows, whole blocks are dropped, counted and shown,
    and the Receiver is told about the gap so packet timing stays right.
    demo: replay the synthetic capture from demo_capture.py (made here on first use, so the window stays live),
    looping. loop: start a file again at its end. radio: the radio settings (this is a separate process)."""
    if radio is not None:
        configure(radio)
    blocks = queue.Queue(maxsize=40)
    stop = threading.Event()
    rec_lock = threading.Lock()
    st = dict(read=0, dropped=0, t0=None, rec=None)
    err_lines = collections.deque(maxlen=5)
    proc = src = reader_thread = None
    try:
        if demo:
            out_q.put(("status", "Preparing the demo…"))
            import demo_capture
            file, file_fc, loop = demo_capture.ensure(), FC, True
        if file:
            src = open(file, "rb")
            out_q.put(("status", f"Replaying {os.path.basename(file)}"))
        else:
            proc, src = open_hackrf(lna, vga)

            def drain():
                for line in iter(proc.stderr.readline, b""):
                    err_lines.append(line.decode(errors="replace").strip())
            threading.Thread(target=drain, daemon=True).start()
            out_q.put(("status", f"HackRF receiving: {FC / 1e6:.1f} MHz, {FS / 1e6:.1f} MS/s, "
                                 f"LNA {lna} dB, VGA {vga} dB, amp off"))

        def reader():
            idx = 0
            while not stop.is_set():
                raw = _read_exact(src, 2 * BLOCK)
                if raw is None and loop and idx > 0:
                    src.seek(0)                           # the stream time keeps counting: no gap, no jump
                    continue
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
        reader_thread = threading.Thread(target=reader, daemon=True)
        reader_thread.start()

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
                    out_q.put(("done", "Replay finished"))
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
        if proc:
            if os.name == "nt" and reader_thread:         # let the reader leave its read before the pipe closes
                reader_thread.join(1)
            stop_hackrf(proc, src)
        elif src is not None:                             # a recording
            if reader_thread:
                reader_thread.join(1)
            src.close()
        with rec_lock:
            if st["rec"]:
                st["rec"].close()


class Worker:
    """Starts and stops the DSP process. Results arrive on the queue passed in."""

    def __init__(self, q, lna, vga, file=None, file_fc=None, realtime=True, loop=False, demo=False):
        self.q = q
        self.ctl = mp.Queue()
        self.p = mp.Process(target=dsp_main, daemon=True,
                            args=(q, self.ctl, lna, vga, file, FC if file_fc is None else file_fc, realtime, loop,
                                  demo, RADIO))

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
    import air_monitor_ui                                # Qt is imported only here, never in the DSP process
    air_monitor_ui.run(args)


def parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--demo", action="store_true", help="play a built-in example recording (no HackRF needed)")
    ap.add_argument("--file", help="replay an int8 IQ capture (6.5 MS/s) instead of the HackRF")
    ap.add_argument("--file-fc", type=float,
                    help="centre frequency of --file in MHz (default: where the HackRF is tuned, 1.5 MHz below "
                         "the channel)")
    ap.add_argument("--config", help="radio settings file (default: air-monitor.toml, if it exists)")
    ap.add_argument("--fast", action="store_true", help="replay --file as fast as possible")
    ap.add_argument("--lna", type=int, choices=[0, 8, 16, 24, 32, 40],
                    help="HackRF LNA gain dB (default: the last sensitivity used, Normal = 16)")
    ap.add_argument("--vga", type=int, help="HackRF VGA gain dB, 0-62, even (default: last used, Normal = 20)")
    ap.add_argument("--details", action=argparse.BooleanOptionalAction,
                    help="open with the expert details shown (or hidden); default: as last time")
    ap.add_argument("--show-other", action="store_true", help="also list non-LoRa bursts (Wi-Fi etc.) in the table")
    ap.add_argument("--autostart", action="store_true", help="start receiving at once")
    ap.add_argument("--screenshot", help=argparse.SUPPRESS)
    ap.add_argument("--screenshot-after", type=float, default=5, help=argparse.SUPPRESS)
    ap.add_argument("--size", help=argparse.SUPPRESS)          # WxH window size, for screenshots
    ap.add_argument("--theme", choices=["light", "dark"], help=argparse.SUPPRESS)   # default: follow the system
    ap.add_argument("--settings", help=argparse.SUPPRESS)      # an .ini file in place of the user's saved choices
    return ap


def main():
    args = parser().parse_args()
    try:
        args.radio = radio_config.load(args.config)
    except radio_config.ConfigError as e:
        sys.exit(f"Air Monitor can't start: there is a problem in the radio settings.\n{e}")
    run_gui(args)


if __name__ == "__main__":
    main()
