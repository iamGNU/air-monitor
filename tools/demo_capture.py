"""A short synthetic capture of the 2440 MHz link, so Air Monitor can be tried without a HackRF.

Three nodes send LoRa packets in fixed time slots (one packet is corrupted on purpose, so it fails its CRC), with a
few Wi-Fi-like and Bluetooth-like bursts and receiver noise. It is written as int8 IQ at 6.5 MS/s centred on
2438.5 MHz, exactly like a capture from the Record button, so the monitor replays it like a real one. The file is a
whole number of 50 ms reads long, so it can loop without a seam.

    python3 tools/demo_capture.py [OUT.iq]          # default: a cached file in the temp folder
"""
import os
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import air_monitor as am  # noqa: E402  (FS, OFFSET, BLOCK)
import lora_phy as phy  # noqa: E402

VERSION = 1                       # bump when the content changes, so cached files are rebuilt
SECONDS = 2.0                     # loop length
FRAME_S = 0.25                    # each node sends once per frame, in its own slot
NOISE = 1.5 / 128                 # receiver noise, per I/Q component (full scale = 1)
NODES = [                         # slot start (s), amplitude (full scale = 1), message
    (0.020, 0.12, "node-1 temp {:.1f} C"),
    (0.100, 0.05, "node-2 hum {:.0f} %"),
    (0.180, 0.025, "node-3 batt {:.2f} V"),
]
BAD = (5, 2)                      # (frame, node) whose packet is corrupted
WIFI = [(1, 0.060, 0.0025), (4, 0.065, 0.0015), (6, 0.058, 0.0030)]       # (frame, offset s, length s)
BT = [(0, 0.140, -1.5e6), (2, 0.145, 3.0e6), (3, 0.150, -1.5e6), (5, 0.140, 3.0e6), (7, 0.142, -1.0e6)]


def default_path():
    return os.path.join(tempfile.gettempdir(), f"air_monitor_demo_v{VERSION}.iq")


def n_samples():
    return int(round(SECONDS * am.FS / am.BLOCK)) * am.BLOCK


def _band_noise(rng, n, f0, width, amp):
    """Complex noise of `n` samples, band-limited to f0 +- width / 2 (Hz, relative to the tuned frequency)."""
    x = rng.standard_normal(n) + 1j * rng.standard_normal(n)
    f = np.fft.fftfreq(n, 1 / am.FS)
    x = np.fft.ifft(np.fft.fft(x) * (np.abs(f - f0) <= width / 2))
    return x * amp / (np.sqrt(np.mean(np.abs(x) ** 2)) + 1e-20)


def build():
    """Return the capture as int8 IQ bytes."""
    rng = np.random.default_rng(1)
    n = n_samples()
    x = (rng.standard_normal(n) + 1j * rng.standard_normal(n)) * NOISE
    shift = 2 * np.pi * am.OFFSET / am.FS
    for fr in range(int(SECONDS / FRAME_S)):
        t0 = fr * FRAME_S
        for k, (slot, amp, msg) in enumerate(NODES):
            value = (21.5 + 0.1 * fr, 48 + fr % 3, 3.92 - 0.01 * fr)[k]
            syms = phy.encode_symbols(msg.format(value).ljust(20).encode())
            if (fr, k) == BAD:
                syms[15] = (syms[15] + 37) % phy.N          # a damaged payload symbol: CRC fails
            # conjugated, then moved up to the channel: see lora_phy.read_iq and the encode command
            z = np.conj(np.array(phy.modulate(syms, am.FS), np.complex128))
            i = int((t0 + slot) * am.FS)
            x[i:i + len(z)] += amp * z * np.exp(1j * shift * (i + np.arange(len(z))))
        for f, off, length in WIFI:                          # wideband bursts across the whole view
            if f == fr:
                i, m = int((t0 + off) * am.FS), int(length * am.FS)
                x[i:i + m] += _band_noise(rng, m, 0.0, 6.0e6, 0.08)
        for f, off, f0 in BT:                                # short narrow hops, outside the LoRa channel
            if f == fr:
                i, m = int((t0 + off) * am.FS), int(0.0004 * am.FS)
                x[i:i + m] += _band_noise(rng, m, f0, 1.0e6, 0.05)
    iq = np.empty(2 * n, np.int8)
    iq[0::2] = np.clip(np.round(x.real * 128), -127, 127)
    iq[1::2] = np.clip(np.round(x.imag * 128), -127, 127)
    return iq.tobytes()


def ensure(path=None):
    """Write the demo capture to `path` (default: the temp folder) unless it is already there. Returns the path."""
    path = path or default_path()
    if os.path.exists(path) and os.path.getsize(path) == 2 * n_samples():
        return path
    tmp = f"{path}.{os.getpid()}.part"
    with open(tmp, "wb") as f:
        f.write(build())
    os.replace(tmp, path)
    return path


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else None
    if out and os.path.exists(out):
        os.remove(out)
    print(ensure(out))
