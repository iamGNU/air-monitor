"""Smoke test for the window, drawn offscreen: the demo, the details view and replaying a recording.
Packet logs and saved choices go to a temp folder, never to logs/ or the user's settings."""
import os
import sys
import tempfile
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import air_monitor as am  # noqa: E402
import demo_capture  # noqa: E402

try:
    from pyqtgraph.Qt import QtWidgets
    import air_monitor_ui as ui
except ImportError:                                   # PyQt6 or pyqtgraph not installed
    ui = None


def packet(seq=None, kind="ok", t=0.0, data=None):
    """A decoded packet as the receiver reports it; by default a v0.1 counter packet (0xA5 + 16-bit counter)."""
    data = bytes([0xA5, seq & 255, seq >> 8]) if data is None else data
    return dict(kind=kind, stream_s=t, wall=time.time(), level_db=-20.0, snr_db=12.0, data=data, dt_ms=97.0,
                dur_ms=2.8, length=len(data), crc_ok=kind == "ok", header_ok=True, cfo_khz=-38.0, sync=(8, 16))


def wait_for(cond, timeout):
    end = time.time() + timeout
    while time.time() < end:
        QtWidgets.QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return cond()


@unittest.skipIf(ui is None, "PyQt6 / pyqtgraph not installed")
class Window(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        cls.c = ui.setup_app(cls.app, "light")
        cls.logs, am.LOGS = am.LOGS, cls.tmp.name
        cls.notify, ui.notify = ui.notify, lambda *a: cls.notes.append(a[1:])   # no real notifications
        cls.notes = []

    @classmethod
    def tearDownClass(cls):
        am.LOGS = cls.logs
        ui.notify = cls.notify
        cls.tmp.cleanup()

    def window(self, *argv):
        args = am.parser().parse_args([*argv, "--settings", os.path.join(self.tmp.name, "settings.ini")])
        w = ui.Main(args, self.c)
        w.show()
        self.addCleanup(lambda: (w.stop(), w.close()))
        return w

    def test_demo(self):
        w = self.window("--demo", "--no-details")
        self.assertEqual(w.stack.currentIndex(), 0, "welcome page before Start")
        w.start()
        self.assertTrue(wait_for(lambda: w.status.headline == "Link is working", 40), w.status)
        self.assertTrue(wait_for(lambda: w.table.rowCount() >= 3, 10))
        messages = [w.table.item(r, 3).text() for r in range(w.table.rowCount())]
        self.assertTrue(any(m.startswith("node-") for m in messages), messages)
        self.assertGreater(int(w.tiles["ok"].v.text()), 0)
        # sensitivity and recording only apply to the live HackRF
        self.assertFalse(any(b.isEnabled() for b in w.sens.buttons()))
        self.assertFalse(w.act_rec.isEnabled())
        # details on and off
        self.assertTrue(w.table.isColumnHidden(len(ui.COLS) - 1))
        w.set_details(True, save=False)
        self.assertFalse(w.table.isColumnHidden(len(ui.COLS) - 1))
        self.assertTrue(all(x.isVisible() for x in w.detail_widgets))
        w.set_details(False, save=False)
        self.assertTrue(w.table.isColumnHidden(len(ui.COLS) - 1))
        self.assertFalse(any(x.isVisible() for x in w.detail_widgets))
        self.assertTrue(wait_for(lambda: len(w.c_hist.getData()[0] or []) > 0, 10), "history drawn")
        w.toggle()                                                       # the user presses Stop
        self.assertEqual(w.status.headline, "Stopped")
        self.assertTrue(w.status.detail.startswith("Session: "), w.status.detail)
        self.assertTrue(w.status_card.link.isVisibleTo(w) and w.status_card.link.text() == "View summary")
        self.assertIsNone(w.last_session.summary_path, "the demo is not logged")

    def test_replay_to_the_end(self):
        w = self.window("--file", demo_capture.ensure(), "--fast", "--no-details")
        before = set(os.listdir(self.tmp.name))
        w.start()
        self.assertTrue(wait_for(lambda: w.status.headline == "Recording finished", 60), w.status)
        self.assertEqual(w.count, {"ok": 23, "bad": 1, "other": 3})
        logs = [f for f in set(os.listdir(self.tmp.name)) - before if f.startswith("air_") and f.endswith(".csv")]
        self.assertEqual(len(logs), 1, logs)
        with open(os.path.join(self.tmp.name, logs[0])) as f:
            self.assertEqual(len(f.readlines()), 1 + 27)
        with open(w.last_session.summary_path) as f:                    # saved beside the packet log
            text = f.read()
        self.assertIn("Packets read:  23", text)
        self.assertIn("Damaged:       1", text)
        self.assertIn(logs[0], text)

    def test_counter_packets_and_loss(self):
        w = self.window("--no-details")                                  # live HackRF source, not started
        w.begin_session()
        w.stack.setCurrentIndex(1)
        for i, (seq, kind) in enumerate([(100, "ok"), (101, "ok"), (None, "bad"), (103, "ok"), (106, "ok")]):
            w.add_packet(packet(seq if seq is not None else 0, kind, t=0.1 * i))
        w.now = 0.5
        w.refresh()
        self.assertEqual(w.table.item(0, 3).text(), "Packet #106")
        bad = w.tiles["bad"]
        self.assertEqual(bad.title.text(), "Lost")
        self.assertEqual(bad.v.text(), "42.9 %")                         # #100-#106: 3 of 7 lost
        self.assertEqual(bad.cap.text(), "1 damaged · 2 not heard")
        self.assertEqual((w.session.ok, w.session.bad, w.session.not_heard), (4, 1, 2))

    def test_waits_for_the_hackrf(self):
        w = self.window("--no-details")
        w.begin_session()
        w.on_error("hackrf_open() failed: HackRF not found (-5)")       # e.g. unplugged
        w.refresh()
        self.assertTrue(w.waiting and w.retry_timer.isActive())
        self.assertEqual((w.status.level, w.status.headline), ("warn", "HackRF not found"))
        self.assertIn("by itself", w.status.detail)
        self.assertEqual(w.btn.text(), "■  Stop")
        w.toggle()                                                       # Stop cancels the waiting...
        self.assertFalse(w.waiting or w.retry_timer.isActive())
        self.assertEqual(w.btn.text(), "▶  Start")
        self.assertEqual((w.status.level, w.status.headline), ("bad", "HackRF not found"))   # ...says why
        self.assertIn("press Start", w.status.detail)
        # an error the user has to fix is not waited for
        w.on_error("hackrf_transfer: not installed (not found on the PATH)")
        self.assertFalse(w.waiting)


if __name__ == "__main__":
    unittest.main()
