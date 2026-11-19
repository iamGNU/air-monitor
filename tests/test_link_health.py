import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import link_health as lh  # noqa: E402


def pk(t, kind="ok", level=-30.0, snr=12.0):
    return (t, kind, level, snr)


def status(**kw):
    base = dict(running=True, have_data=True, now=5.0)
    base.update(kw)
    return lh.link_status(**base)


class Ratings(unittest.TestCase):
    def test_signal(self):
        self.assertEqual(lh.signal_rating(None).bars, 0)
        self.assertEqual(lh.signal_rating(-11.9), lh.Rating(5, "Too strong", "bad"))
        self.assertEqual(lh.signal_rating(-12.0).word, "Strong")       # the threshold itself is still fine
        self.assertEqual(lh.signal_rating(-30).word, "Good")
        self.assertEqual(lh.signal_rating(-50).word, "Low")
        self.assertEqual(lh.signal_rating(-90).bars, 1)

    def test_quality(self):
        self.assertEqual(lh.quality_rating(None).bars, 0)
        self.assertEqual(lh.quality_rating(10).word, "Excellent")
        self.assertEqual(lh.quality_rating(9.9).word, "Good")
        self.assertEqual(lh.quality_rating(0).level, "ok")
        self.assertEqual(lh.quality_rating(-0.1).level, "warn")
        self.assertEqual(lh.quality_rating(-9).word, "Very weak")


class Sensitivity(unittest.TestCase):
    def test_presets(self):
        self.assertEqual(lh.preset_name(16, 20), "Normal")
        self.assertIsNone(lh.preset_name(24, 20))

    def test_step(self):
        self.assertEqual(lh.step_sensitivity(16, 20, -1), "Low")
        self.assertEqual(lh.step_sensitivity(16, 20, +1), "High")
        self.assertIsNone(lh.step_sensitivity(*lh.SENSITIVITY["Low"], -1))
        self.assertIsNone(lh.step_sensitivity(*lh.SENSITIVITY["High"], +1))
        self.assertEqual(lh.step_sensitivity(24, 24, -1), "Normal")      # custom gain: nearest preset below

    def test_advice(self):
        self.assertIsNone(lh.gain_advice([]))
        self.assertIsNone(lh.gain_advice([pk(1, "other", level=-5)]))   # only LoRa packets count
        self.assertEqual(lh.gain_advice([pk(1), pk(2, level=-8)]), "lower")
        self.assertEqual(lh.gain_advice([pk(1, snr=-3), pk(2, snr=-2), pk(3, snr=4)]), "raise")
        self.assertIsNone(lh.gain_advice([pk(1), pk(2)]))


class Status(unittest.TestCase):
    def test_order(self):
        self.assertEqual(status(error="hackrf_open() failed: HackRF not found (-5)").headline, "HackRF not found")
        self.assertEqual(status(running=False, finished=True).headline, "Recording finished")
        self.assertEqual(status(running=False).headline, "Stopped")
        self.assertEqual(status(have_data=False).headline, "Starting…")

    def test_working(self):
        s = status(recent=[pk(4.0), pk(4.5), pk(4.8)])
        self.assertEqual((s.level, s.headline, s.action), ("ok", "Link is working", None))
        self.assertIn("0.6 packets/s", s.detail)
        self.assertIn("just now", s.detail)

    def test_rate_early_in_a_run(self):
        self.assertIn("2.0 packets/s", status(now=1.0, recent=[pk(0.2), pk(0.6)]).detail)

    def test_too_strong(self):
        s = status(recent=[pk(4.0, level=-8)])
        self.assertEqual((s.level, s.action), ("bad", "lower"))
        s = status(recent=[pk(4.0, level=-8)], lna=8, vga=10)              # already at Low
        self.assertEqual((s.level, s.action), ("bad", None))
        self.assertIn("further", s.detail)
        s = status(recent=[pk(4.0, level=-8)], can_tune=False)            # a recording
        self.assertIsNone(s.action)
        self.assertIn("recording", s.detail)

    def test_data_lost(self):
        s = status(recent=[pk(4.0)], data_lost=True)
        self.assertEqual((s.level, s.headline), ("warn", "Some radio data was lost"))

    def test_damaged(self):
        s = status(recent=[pk(1), pk(2, "bad"), pk(3, "bad"), pk(4)])
        self.assertEqual((s.level, s.headline, s.action), ("warn", "Some packets are damaged", None))
        self.assertIn("2 of 4", s.detail)
        self.assertEqual(status(recent=[pk(1), pk(2, "bad"), pk(3), pk(4)]).level, "ok")   # one bad: still fine

    def test_damaged_and_weak(self):
        s = status(recent=[pk(1, "bad", snr=-4), pk(2, "bad", snr=-3), pk(3, snr=-2)])
        self.assertEqual((s.headline, s.action), ("Some packets are damaged", "raise"))

    def test_weak_but_working(self):
        s = status(recent=[pk(4, snr=-2), pk(4.5, snr=-1)])
        self.assertEqual((s.level, s.action), ("warn", "raise"))
        s = status(recent=[pk(4, snr=-2)], lna=32, vga=30)                # already at High
        self.assertIsNone(s.action)

    def test_listening(self):
        s = status()
        self.assertEqual((s.level, s.headline), ("idle", "Listening, no LoRa packets yet"))
        self.assertIn("quiet", s.detail)
        s = status(recent=[pk(4, "other")], ever_lora=True)
        self.assertEqual((s.level, s.headline), ("warn", "No LoRa packets in the last 5 s"))
        self.assertIn("Wi-Fi", s.detail)


class Errors(unittest.TestCase):
    def test_messages(self):
        cases = {
            "FileNotFoundError: [Errno 2] No such file or directory: 'hackrf_transfer'": "isn't installed",
            "hackrf_open() failed: HackRF not found (-5)": "not found",           # libhackrf 2026.01
            "hackrf_open() failed: HACKRF_ERROR_NOT_FOUND (-5)": "not found",     # older libhackrf
            "No HackRF boards found.": "not found",
            "hackrf_open() failed: HackRF busy (-6)": "in use",
            "hackrf_open() failed: Resource busy (-1000)": "in use",              # seen with the device held open
            "hackrf_open() failed: USB error (-1000) | LIBUSB_ERROR_ACCESS": "permission",
            "hackrf_open() failed: USB error (-1000)": "couldn't be opened",
            "FileNotFoundError: [Errno 2] No such file or directory: '/x/cap.iq'": "Recording not found",
            "hackrf_transfer stopped": "stopped unexpectedly",
            "": "stopped unexpectedly",
        }
        for raw, want in cases.items():
            self.assertIn(want, lh.friendly_error(raw)[0], raw)


if __name__ == "__main__":
    unittest.main()
