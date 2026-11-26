import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import payloads as pl  # noqa: E402


class Describe(unittest.TestCase):
    def test_counter(self):
        d = pl.describe(bytes.fromhex("a5d708"))                       # seen on the bench
        self.assertEqual((d.shown, d.seq, d.stream, d.style), ("Packet #2263", 0x08D7, 0xA5, "counter"))
        self.assertEqual(pl.describe(bytes.fromhex("a5ffff")).seq, 65535)

    def test_text(self):
        self.assertEqual(pl.describe(b"node-1 temp 21.5 C  ")[::3], ("node-1 temp 21.5 C", "text"))
        self.assertEqual(pl.describe(b"hello #000001\0").shown, "hello #000001")   # lora_phy test packets

    def test_hex(self):
        self.assertEqual(pl.describe(bytes.fromhex("b5d708"))[::3], ("b5 d7 08", "hex"))   # other first byte
        self.assertEqual(pl.describe(bytes.fromhex("a5d70800"))[::3], ("a5 d7 08 00", "hex"))  # other length
        self.assertEqual(pl.describe(b"\0\0").shown, "00 00")


class Tracker(unittest.TestCase):
    def feed(self, events):
        t = pl.SequenceTracker()
        for e in events:
            t.bad() if e == "bad" else t.good(0xA5, e)
        return t

    def test_no_loss(self):
        t = self.feed([10, 11, 12, 13])
        self.assertEqual((t.received, t.lost, t.loss_pct), (4, 0, 0.0))
        self.assertTrue(t.active)
        self.assertFalse(pl.SequenceTracker().active)

    def test_damaged_fill_the_gap(self):
        # as on the bench: every gap was a damaged packet
        t = self.feed([10, 11, "bad", 13, "bad", "bad", 16])
        self.assertEqual((t.received, t.damaged, t.not_heard), (4, 3, 0))
        self.assertAlmostEqual(t.loss_pct, 100 * 3 / 7)

    def test_not_heard(self):
        t = self.feed([10, "bad", 14])                                  # 3 missing, 1 of them heard damaged
        self.assertEqual((t.damaged, t.not_heard), (1, 2))

    def test_wrap_and_duplicates(self):
        t = self.feed([65534, 65535, 0, 1, 1])
        self.assertEqual((t.received, t.lost), (4, 0))

    def test_restart(self):
        t = self.feed([5000, 5001, 3, 4])                               # the node rebooted: counter back to 0
        self.assertEqual((t.received, t.lost, t.restarts), (4, 0, 1))
        t = self.feed([5, 5 + pl.RESTART_JUMP + 2])                     # implausibly far ahead
        self.assertEqual((t.lost, t.restarts), (0, 1))

    def test_new_run_forgets_the_gap(self):
        t = self.feed([10, 11])
        t.new_run()                                                     # sensitivity change: ~1 s not listening
        t.good(0xA5, 21)
        self.assertEqual((t.received, t.lost), (3, 0))

    def test_streams_are_separate(self):
        t = pl.SequenceTracker()
        for s, n in [(1, 100), (2, 7), (1, 101), (2, 8), (1, 103)]:
            t.good(s, n)
        self.assertEqual((t.received, t.not_heard), (5, 1))


if __name__ == "__main__":
    unittest.main()
