import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import radio_config as rc  # noqa: E402


class Load(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def write(self, text):
        path = os.path.join(self.dir.name, "air-monitor.toml")
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_defaults(self):
        self.assertEqual(rc.DEFAULT, rc.Radio(2440e6, 7, 1_625_000, 5))
        with self.assertRaises(rc.ConfigError):
            rc.load(os.path.join(self.dir.name, "missing.toml"))          # an explicit file must exist

    def test_file(self):
        r = rc.load(self.write("[radio]\nfrequency_mhz = 2425.5\nspreading_factor = 9\nbandwidth_khz = 812.5\n"
                               "coding_rate = 6\n"))
        self.assertEqual(r, rc.Radio(2425.5e6, 9, 812_500, 6))
        self.assertEqual(r.label(), "2425.5 MHz · SF9 · 812.5 kHz · CR 4/6")
        self.assertEqual(rc.load(self.write("[radio]\nspreading_factor = 8\n")), rc.Radio(sf=8))   # partial

    def test_bad_values(self):
        for text, word in [("spreading_factor = 13", "spreading_factor"), ("bandwidth_khz = 500", "bandwidth_khz"),
                           ("frequency_mhz = 915", "frequency_mhz"), ("coding_rate = 4", "coding_rate"),
                           ("spreading_factor = 'seven'", "seven"), ("spreding_factor = 7", "unknown"),
                           ("spreading_factor = ", "")]:
            with self.assertRaises(rc.ConfigError, msg=text) as e:
                rc.load(self.write(f"[radio]\n{text}\n"))
            self.assertIn(word, str(e.exception))

    def test_several_errors_at_once(self):
        with self.assertRaises(rc.ConfigError) as e:
            rc.load(self.write("[radio]\nspreading_factor = 13\nbandwidth_khz = 500\n"))
        self.assertEqual(len(str(e.exception).splitlines()), 2)


class Derived(unittest.TestCase):
    def test_default_is_what_the_monitor_always_used(self):
        r = rc.DEFAULT
        self.assertEqual((r.os, r.fs, r.offset, r.cutoff, r.block), (4, 6.5e6, 1.5e6, 0.95e6, 325_000))

    def test_narrow_bandwidths(self):
        for bw, os_, offset in [(812_500, 4, 750e3), (406_250, 8, 400e3), (203_125, 16, 400e3)]:
            r = rc.Radio(bw=bw)
            self.assertEqual((r.os, r.fs, r.offset), (os_, 3.25e6, offset), bw)
            self.assertEqual(r.block, 162_500)
            self.assertLess(r.offset + bw / 2, r.fs / 2)                 # the channel fits in the HackRF band
            self.assertGreater(r.offset - bw / 2, 0)                     # and stays clear of its DC spike


if __name__ == "__main__":
    unittest.main()
