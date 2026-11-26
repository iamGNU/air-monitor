"""Regression test for the receiver: decode the demo recording and check every packet.

The demo (tools/demo_capture.py) has three nodes sending in fixed slots every 250 ms, one deliberately damaged
packet and three Wi-Fi-like bursts per 2 s loop. If this test fails after a change, the receiver's behaviour changed.
"""
import collections
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import air_monitor as am  # noqa: E402
import demo_capture as demo  # noqa: E402
import radio_config as rc  # noqa: E402

LEVEL_DB = [-18.5, -26.0, -34.2]          # per node, as measured when the test was written
OTHER_LEVEL_DB = -27.1


def expected_packets():
    """(stream_s, kind, node, payload) for one loop, straight from the demo's definition."""
    out = []
    frame_s, frames, bad_frame = demo.timing()
    for fr in range(frames):
        for k, (slot, _, msg) in enumerate(demo.NODES):
            value = (21.5 + 0.1 * fr, 48 + fr % 3, 3.92 - 0.01 * fr)[k]
            kind = "bad" if (fr, k) == (bad_frame, demo.BAD[1]) else "ok"
            out.append((fr * frame_s + slot * frame_s / demo.REF_FRAME_S, kind, k,
                        msg.format(value).ljust(20).encode()))
    return out


def decode_demo(loops=2):
    data = demo.build()
    rx = am.Receiver()
    packets = []
    step = 2 * am.BLOCK
    for _ in range(loops):                                    # two loops: the seam must not lose anything
        for i in range(0, len(data) - step + 1, step):
            packets += rx.process(data[i:i + step])["packets"]
    return packets


class DemoDecode(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.packets = decode_demo()

    def loop(self, n):
        return [p for p in self.packets if n * demo.seconds() <= p["stream_s"] < (n + 1) * demo.seconds()]

    def test_counts_per_loop(self):
        for n in range(2):
            kinds = collections.Counter(p["kind"] for p in self.loop(n))
            self.assertEqual(kinds, {"ok": 23, "bad": 1, "other": 3}, f"loop {n}")

    def test_every_lora_packet(self):
        want = expected_packets()
        for n in range(2):
            got = [p for p in self.loop(n) if p["kind"] != "other"]
            self.assertEqual(len(got), len(want))
            for p, (t, kind, node, payload) in zip(got, want):
                self.assertAlmostEqual(p["stream_s"], n * demo.seconds() + t, delta=0.002)
                self.assertEqual(p["kind"], kind)
                self.assertAlmostEqual(p["level_db"], LEVEL_DB[node], delta=0.5)
                self.assertGreater(p["snr_db"], 5)
                self.assertEqual(p["length"], 20)
                if kind == "ok":
                    self.assertEqual(p["data"], payload)
                    self.assertTrue(p["crc_ok"] and p["header_ok"])
                else:
                    self.assertFalse(p["crc_ok"])

    def test_intervals(self):
        dts = [p["dt_ms"] for p in self.packets if p["kind"] != "other"][1:]
        self.assertTrue(all(abs(d - 80) < 0.5 or abs(d - 90) < 0.5 for d in dts), dts)

    def test_other_bursts(self):
        others = [p for p in self.packets if p["kind"] == "other"]
        self.assertEqual(len(others), 6)
        for p in others:
            self.assertAlmostEqual(p["level_db"], OTHER_LEVEL_DB, delta=0.5)
            self.assertTrue(1.4 < p["dur_ms"] < 3.2, p["dur_ms"])
            self.assertEqual(p["data"], b"")


def capture(packets, seconds):
    """int8 IQ like the HackRF's: (start_s, payload, amplitude) packets over the demo's receiver noise."""
    import numpy as np
    import lora_phy as phy
    rng = np.random.default_rng(3)
    n = int(np.ceil(seconds * am.FS / am.BLOCK)) * am.BLOCK
    x = (rng.standard_normal(n) + 1j * rng.standard_normal(n)) * demo.NOISE
    shift = 2 * np.pi * am.OFFSET / am.FS
    for t, payload, amp in packets:
        z = np.conj(np.array(phy.modulate(phy.encode_symbols(payload), am.FS)))
        i = int(t * am.FS)
        x[i:i + len(z)] += amp * z * np.exp(1j * shift * (i + np.arange(len(z))))
    iq = np.empty(2 * n, np.int8)
    iq[0::2] = np.clip(np.round(x.real * 128), -127, 127)
    iq[1::2] = np.clip(np.round(x.imag * 128), -127, 127)
    rx, out = am.Receiver(), []
    data = iq.tobytes()
    for i in range(0, len(data) - 2 * am.BLOCK + 1, 2 * am.BLOCK):
        out += rx.process(data[i:i + 2 * am.BLOCK])["packets"]
    return [p for p in out if p["kind"] != "other"]


def data_packet(seq, length):
    """A v0.1 data packet: 0xA4, 16-bit counter, then data."""
    return bytes([0xA4, seq & 255, seq >> 8]) + bytes((7 * i + seq) & 255 for i in range(length - 3))


WEAK, STRONG = 0.02, 0.12                     # amplitudes: found by preamble (SNR ~7 dB) / by energy (~18 dB)


class LongPackets(unittest.TestCase):
    """Data packets of 140-199 bytes (seen from the v0.1 nodes on 26-11-2026) and the longest LoRa allows."""

    def test_weak_long_packets(self):            # a weak packet's end can't be seen: it needs a long enough window
        sent = [(0.05 + 0.1 * k, data_packet(k, 199), WEAK) for k in range(4)]
        got = capture(sent, 0.6)
        self.assertEqual([p["data"] for p in got if p["kind"] == "ok"], [d for _, d, _ in sent])

    def test_longest_packet(self):
        sent = [(0.05 + 0.1 * k, data_packet(k, 255), a) for k, a in enumerate((STRONG, WEAK))]
        got = capture(sent, 0.4)
        self.assertEqual([p["data"] for p in got if p["kind"] == "ok"], [d for _, d, _ in sent])

    def test_beacon_right_after_a_weak_data_packet(self):     # the nodes send a beacon ~40 ms after data
        sent = []
        for k in range(3):
            t = 0.05 + 0.15 * k
            sent += [(t, data_packet(2 * k, 150), WEAK), (t + 0.040, bytes([0xA5, 2 * k + 1, 0]), WEAK)]
        got = capture(sent, 0.6)
        self.assertEqual([p["data"] for p in got if p["kind"] == "ok"], [d for _, d, _ in sent])


class OtherRadioSettings(unittest.TestCase):
    """The demo made and decoded at other settings (air-monitor.toml): every LoRa packet of the first loop."""

    def check(self, radio):
        am.configure(rc.validate(radio))
        try:
            packets, loop_s, expected = decode_demo(), demo.seconds(), expected_packets()
        finally:
            am.configure(rc.DEFAULT)
        got = [(p["kind"], p["data"]) for p in packets if p["kind"] != "other" and p["stream_s"] < loop_s]
        want = [(kind, payload) for _, kind, _, payload in expected]
        self.assertEqual([k for k, _ in got], [k for k, _ in want])
        self.assertEqual([d for (k, d) in got if k == "ok"], [d for (k, d) in want if k == "ok"])

    def test_sf8(self):
        self.check(rc.Radio(sf=8))

    def test_812_khz(self):
        self.check(rc.Radio(bw=812_500))

    def test_sf10_203_khz(self):                              # the slowest supported: 5 ms symbols
        self.check(rc.Radio(sf=10, bw=203_125, cr=8))

    def test_demo_timing(self):
        self.assertEqual(demo.timing(), (0.25, 8, 5))          # the default demo is what it always was
        self.assertEqual(demo.n_samples(), 40 * am.BLOCK)

    def test_default_restored(self):
        self.assertEqual((am.FS, am.FC, am.BLOCK, am.N, am.CUTOFF), (6.5e6, 2438.5e6, 325_000, 128, 0.95e6))


if __name__ == "__main__":
    unittest.main()
