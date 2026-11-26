import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import session as se  # noqa: E402

T0 = 1_700_000_000.0           # a wall time on a 5 s boundary


def run(s, start, seconds, every=0.1, gain="Normal", kind="ok", level=-20.0, snr=15.0):
    for i in range(round(seconds / every)):
        s.add(start + i * every, kind, level, snr, gain, counter=True)


class Totals(unittest.TestCase):
    def test_counts_and_loss(self):
        s = se.Session("demo", T0)
        self.assertFalse(s.active)
        run(s, T0, 10)                                         # 100 good packets
        s.add(T0 + 10, "bad", -20, 2, "Normal")
        s.add(T0 + 10.2, "ok", -20, 15, "Normal", not_heard=3, counter=True)
        s.add(T0 + 10.3, "other")
        self.assertTrue(s.active)
        self.assertEqual((s.ok, s.bad, s.not_heard, s.other, s.lost), (101, 1, 3, 1, 4))
        self.assertAlmostEqual(s.loss_pct, 100 * 4 / 105)

    def test_gap_ignores_restarts(self):
        s = se.Session("x", T0)
        s.add(T0, "ok", -20, 10)
        s.add(T0 + 0.5, "ok", -20, 10)
        s.new_run()                                            # sensitivity change: off for a while
        s.add(T0 + 5, "ok", -20, 10)
        s.add(T0 + 6.2, "ok", -20, 10)
        self.assertAlmostEqual(s.longest_gap, 1.2)

    def test_best_gain(self):
        s = se.Session("x", T0)
        run(s, T0, 5, gain="Normal (LNA 16, VGA 20)")
        for i in range(5):
            s.add(T0 + 6 + i, "bad", -9, 15, "Normal (LNA 16, VGA 20)")
        self.assertIsNone(s.best_gain())                       # only one sensitivity used
        run(s, T0 + 20, 5, gain="Low (LNA 8, VGA 10)")
        self.assertEqual(s.best_gain(), "Low (LNA 8, VGA 10)")


class History(unittest.TestCase):
    def test_bins(self):
        s = se.Session("x", T0)
        run(s, T0, 10, every=0.25)                              # 4 packets/s for 10 s
        s.add(T0 + 30, "bad", -20, 2, "Normal")                 # then silence, one damaged packet
        h = s.history(T0 + 41)
        self.assertEqual(len(h), 8)                             # 8 whole bins since the start
        late = se.Session("x", T0 + 2)                          # started 2 s into a bin: that bin is left out
        self.assertEqual(len(late.history(T0 + 41)), 7)
        self.assertEqual([round(r, 1) for _, r, _ in h], [4.0, 4.0, 0, 0, 0, 0, 0, 0])
        self.assertEqual([lost for _, _, lost in h], [0, 0, 0, 0, 0, 0, 1, 0])
        self.assertAlmostEqual(h[-1][0], -(41 - 37.5) / 60)     # centre of the last whole bin, in minutes

    def test_ten_minutes(self):
        s = se.Session("x", T0)
        run(s, T0, 900, every=1.0)
        h = s.history(T0 + 900)
        self.assertEqual(len(h), 120)
        self.assertGreater(h[0][0], -10.0)
        self.assertLessEqual(len(s.bins), 122)                  # old bins are dropped


class Text(unittest.TestCase):
    def test_summary(self):
        s = se.Session("live from the HackRF", T0)
        run(s, T0, 60, gain="Normal (LNA 16, VGA 20)")
        s.add(T0 + 61, "ok", -9.5, 18, "Normal (LNA 16, VGA 20)", not_heard=2, counter=True)
        s.log_path = "logs/air_x.csv"
        s.end = T0 + 62
        txt = s.summary_text(T0 + 100)
        for want in ("Packets read:  601", "Lost:          2 (0.3 %): 0 damaged, 2 not heard", "1 min 2 s",
                     "peak -9.5 dBFS", "Normal (LNA 16, VGA 20): 601 read, 0.3 % lost", "logs/air_x.csv"):
            self.assertIn(want, txt)
        self.assertEqual(s.one_line(0), "Session: 1 min 2 s · 601 packets · 0.3 % lost")

    def test_no_counters(self):
        s = se.Session("demo", T0)
        s.add(T0, "bad", -20, 3, "")
        self.assertIn("Damaged:       1", s.summary_text(T0 + 1))
        self.assertTrue(s.one_line(T0 + 1).endswith("1 damaged"))


if __name__ == "__main__":
    unittest.main()
