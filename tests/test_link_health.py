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


class Summary(unittest.TestCase):
    def test_window_summary(self):
        self.assertIsNone(lh.window_summary([]))
        self.assertIsNone(lh.window_summary([pk(1, "other")]))
        self.assertEqual(lh.window_summary([pk(1, level=-20, snr=5), pk(2, "bad", level=-9, snr=15),
                                            pk(3, level=-30, snr=12), pk(4, "other", level=-3)]), (-9, 12))

    def test_tile_agrees_with_status(self):
        # seen on the bench: the last packet at -12.9 dBFS ("Strong"), an earlier one at -11.3 ("Too strong")
        recent = [pk(1.0, level=-11.3), pk(1.1, level=-12.9)]
        self.assertEqual(status(recent=recent).headline, "Signal too strong")
        self.assertEqual(lh.signal_rating(lh.window_summary(recent)[0]).word, "Too strong")


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

    def test_lost_not_heard(self):
        # the counter says 2 packets were never heard, one more was damaged
        s = status(recent=[pk(1), pk(2, "bad"), pk(2.1, "missed"), pk(2.2, "missed"), pk(3), pk(4)])
        self.assertEqual((s.level, s.headline), ("warn", "Some packets are lost"))
        self.assertIn("3 of 6", s.detail)
        self.assertIn("1 damaged, 2 not heard", s.detail)

    def test_loss_in_detail(self):
        s = status(recent=[pk(4.0), pk(4.5)], loss_pct=0.4)
        self.assertEqual(s.level, "ok")
        self.assertTrue(s.detail.endswith("· 0.4 % lost"), s.detail)
        self.assertNotIn("lost", status(recent=[pk(4.0)]).detail)        # no counter: no loss figure

    def test_loss_level(self):
        self.assertEqual([lh.loss_level(p) for p in (0, 0.9, 1, 4.9, 5)], ["ok", "ok", "warn", "warn", "bad"])

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


class Retry(unittest.TestCase):
    def test_should_retry(self):
        for raw in ("hackrf_open() failed: HackRF not found (-5)", "hackrf_open() failed: Resource busy (-1000)",
                    "hackrf_open() failed: USB error (-1000)", "hackrf_transfer stopped"):
            self.assertTrue(lh.should_retry(raw), raw)
        for raw in ("hackrf_transfer: not installed (not found on the PATH)",
                    "hackrf_open() failed: USB error (-1000) | LIBUSB_ERROR_ACCESS"):
            self.assertFalse(lh.should_retry(raw), raw)

    def test_waiting_status(self):
        s = lh.waiting_status("hackrf_open() failed: HackRF not found (-5)")
        self.assertEqual((s.level, s.headline), ("warn", "HackRF not found"))
        self.assertIn("Plug the HackRF in", s.detail)
        self.assertIn("by itself", s.detail)
        self.assertIn("Gqrx", lh.waiting_status("hackrf_open() failed: Resource busy (-1000)").detail)


class Alert(unittest.TestCase):
    def test_drop_and_back(self):
        a = lh.LinkAlert()
        events = [a.update(t, has) for t, has in [(0, True), (3, True), (6, True), (8, False), (9, False),
                                                  (10, True), (11, True)]]
        self.assertEqual(events, [None, None, None, "dropped", None, "back", None])

    def test_short_link_is_not_alerted(self):
        a = lh.LinkAlert()
        self.assertEqual([a.update(t, has) for t, has in [(0, True), (2, True), (3, False), (20, False)]],
                         [None] * 4)

    def test_reset(self):
        a = lh.LinkAlert()
        a.update(0, True)
        a.update(10, True)
        a.reset()                                     # receiver restarted: no packets for a moment is expected
        self.assertIsNone(a.update(11, False))


class FindBest(unittest.TestCase):
    def trial(self, ok, bad=0, not_heard=0, peak=-30.0, snr=10.0):
        return dict(ok=ok, bad=bad, not_heard=not_heard, peak=peak if ok + bad else None, snr=snr)

    def test_bench_2026_11_25(self):
        # Normal: 144 read, 6 damaged, peak -9 dBFS. Low: 150 read, 0 damaged, peak -34.5 dBFS, SNR 11.
        name, why = lh.pick_sensitivity({"Low": self.trial(40, peak=-34.5, snr=11.2),
                                         "Normal": self.trial(38, 2, peak=-9.1, snr=18.8),
                                         "High": self.trial(30, 9, peak=-3.0, snr=20)})
        self.assertEqual(name, "Low")
        self.assertIn("Normal: 5 % lost, too strong", why)

    def test_lowest_loss_then_snr(self):
        t = {"Low": self.trial(40, 4, snr=3), "Normal": self.trial(40, 0, snr=8), "High": self.trial(40, 0, snr=12)}
        self.assertEqual(lh.pick_sensitivity(t)[0], "High")

    def test_nothing_heard(self):
        name, why = lh.pick_sensitivity({n: self.trial(0) for n in lh.ORDER})
        self.assertIsNone(name)
        self.assertIn("No packets heard", why)

    def test_all_too_strong(self):
        name, why = lh.pick_sensitivity({n: self.trial(40, peak=-5) for n in lh.ORDER})
        self.assertEqual(name, "Low")
        self.assertIn("further", why)


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
            for windows in (False, True):
                self.assertIn(want, lh.friendly_error(raw, windows)[0], raw)

    def test_advice_per_platform(self):
        missing = "hackrf_transfer: not installed (not found on the PATH)"
        self.assertIn("sudo apt install hackrf", lh.friendly_error(missing, windows=False)[1])
        self.assertIn("PATH", lh.friendly_error(missing, windows=True)[1])
        self.assertNotIn("sudo", lh.friendly_error(missing, windows=True)[1])
        # Windows names neither the program nor the file when it can't start it
        self.assertEqual(lh.friendly_error("FileNotFoundError: [WinError 2] The system cannot find the file "
                                           "specified", windows=True)[0], "HackRF software isn't installed")
        self.assertIn("Zadig", lh.friendly_error("hackrf_open() failed: HackRF not found (-5)", windows=True)[1])
        self.assertNotIn("Zadig", lh.friendly_error("hackrf_open() failed: HackRF not found (-5)", False)[1])


if __name__ == "__main__":
    unittest.main()
