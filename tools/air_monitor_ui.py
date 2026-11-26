"""Air Monitor window: one calm screen that shows what is happening on the nodes' channel (2440 MHz).

Simple view (the default): a status line in plain words, four numbers, the waterfall ("what's on the air", last
10 s) with a mark for every packet, the packet strip (last 2 s) and the decoded messages. "Show details" adds the
spectrum, the raw HackRF gains, the technical columns and the receiver health.

The receiver runs in its own process (air_monitor.py); this file only draws. The plain-language rules (status,
ratings, sensitivity, errors) are in link_health.py.
"""
import collections
import csv
import datetime
import html
import multiprocessing as mp
import os
import queue
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtGui, QtWidgets

import air_monitor as am
import link_health as lh
import payloads
from session import Session

Qt = QtCore.Qt
WF_ROWS = 200                         # waterfall rows, one per 50 ms read: 10 s
TL_S = 2.0                            # length of the packet strip (s)
LNA_STEPS = [0, 8, 16, 24, 32, 40]
MAX_ROWS = 500
COLS = ["Time", "Status", "Quality", "Message", "Δt ms", "Len", "CRC", "Level dBFS", "SNR dB", "Freq err kHz",
        "Dur ms", "Hex"]
SIMPLE_COLS = 4                       # the simple view shows the first four columns
MEASURE_S = 4.0                       # "Find best": seconds of packets measured at each sensitivity

LIGHT = dict(bg="#f4f5f7", card="#ffffff", border="#e1e4e8", text="#1c2026", muted="#6b7280", accent="#2563eb",
             accent_hover="#1d4ed8", sel="#e6eefc", ok="#2e9d4f", warn="#c27c00", bad="#d9534f", idle="#8a929c",
             other="#9aa0a6")
DARK = dict(bg="#121417", card="#1b1e22", border="#2c3036", text="#e6e8eb", muted="#9aa2ad", accent="#3b82f6",
            accent_hover="#2563eb", sel="#243247", ok="#3fb565", warn="#e0a030", bad="#e5605c", idle="#8a929c",
            other="#8a8f96")


def notify(window, title, body):
    """Get the user's attention: a desktop notification, a taskbar flash and a beep. Linux: notify-send. Windows
    (and anywhere without notify-send): a pop-up from a system tray icon, when there is a tray."""
    if shutil.which("notify-send"):
        try:
            subprocess.Popen(["notify-send", "--app-name=Air Monitor", "--icon=network-wireless", title, body],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass
    elif QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
        tray = getattr(window, "_tray", None)
        if tray is None:
            icon = window.style().standardIcon(QtWidgets.QStyle.StandardPixmap.SP_MessageBoxInformation)
            tray = window._tray = QtWidgets.QSystemTrayIcon(icon, window)
            tray.setToolTip("Air Monitor")
            tray.show()
        tray.showMessage(title, body)
    QtWidgets.QApplication.alert(window)
    QtWidgets.QApplication.beep()


def tip(text):
    return f"<p>{text}</p>"                     # rich text, so long tooltips wrap


def named(widget, name):
    widget.setObjectName(name)
    return widget


def mix(a, b, t):
    """Colour a moved towards colour b by t (0..1)."""
    qa, qb = QtGui.QColor(a), QtGui.QColor(b)
    return QtGui.QColor(*(round(x + (y - x) * t) for x, y in zip(qa.getRgb()[:3], qb.getRgb()[:3]))).name()


# --------------------------------------------------------------------------------------------- theme
def is_dark(app):
    try:
        scheme = app.styleHints().colorScheme()
        if scheme != Qt.ColorScheme.Unknown:
            return scheme == Qt.ColorScheme.Dark
    except AttributeError:                       # Qt before 6.5
        pass
    return app.palette().color(QtGui.QPalette.ColorRole.Window).lightness() < 128


def palette(c):
    p = QtGui.QPalette()
    R = QtGui.QPalette.ColorRole
    for role, key in ((R.Window, "bg"), (R.WindowText, "text"), (R.Base, "card"), (R.AlternateBase, "bg"),
                      (R.Text, "text"), (R.Button, "card"), (R.ButtonText, "text"), (R.ToolTipBase, "card"),
                      (R.ToolTipText, "text"), (R.Highlight, "accent"), (R.PlaceholderText, "muted"),
                      (R.Mid, "border"), (R.Light, "card"), (R.Dark, "border")):
        p.setColor(role, QtGui.QColor(c[key]))
    p.setColor(R.HighlightedText, QtGui.QColor("#ffffff"))
    for role in (R.Text, R.ButtonText, R.WindowText):
        p.setColor(QtGui.QPalette.ColorGroup.Disabled, role, QtGui.QColor(c["muted"]))
    return p


def stylesheet(c):
    return f"""
QWidget#root {{ background: {c['bg']}; }}
QLabel {{ background: transparent; }}
QFrame#card {{ background: {c['card']}; border: 1px solid {c['border']}; border-radius: 8px; }}
QLabel#title {{ font-size: 15pt; font-weight: 600; }}
QLabel#headline {{ font-size: 13pt; font-weight: 600; }}
QLabel#section {{ font-weight: 600; }}
QLabel#muted {{ color: {c['muted']}; }}
QLabel#hint, QLabel#tileTitle {{ color: {c['muted']}; font-size: 9pt; }}
QLabel#tileValue {{ font-size: 17pt; font-weight: 600; }}
QLabel#rec {{ color: {c['bad']}; font-weight: 600; }}
QLabel#stepNum {{ background: {c['accent']}; color: #ffffff; border-radius: 12px; font-weight: 600; }}
QPushButton {{ background: {c['card']}; color: {c['text']}; border: 1px solid {c['border']}; border-radius: 6px;
              padding: 6px 14px; }}
QPushButton:hover {{ border-color: {c['muted']}; }}
QPushButton:disabled {{ color: {c['muted']}; background: {c['bg']}; }}
QPushButton#primary {{ background: {c['accent']}; color: #ffffff; border: none; font-weight: 600; padding: 8px 18px; }}
QPushButton#primary:hover {{ background: {c['accent_hover']}; }}
QPushButton#seg {{ border-radius: 0; padding: 5px 16px; }}
QPushButton#seg[pos="first"] {{ border-top-left-radius: 6px; border-bottom-left-radius: 6px; }}
QPushButton#seg[pos="last"] {{ border-top-right-radius: 6px; border-bottom-right-radius: 6px; }}
QPushButton#seg:checked {{ background: {c['accent']}; color: #ffffff; border-color: {c['accent']}; }}
QPushButton#seg:checked:disabled {{ background: {c['border']}; color: {c['muted']}; border-color: {c['border']}; }}
QPushButton#link {{ background: transparent; border: none; color: {c['accent']}; padding: 4px 6px; }}
QPushButton#link:hover {{ text-decoration: underline; }}
QToolButton#more {{ background: transparent; border: none; font-size: 17pt; padding: 0 8px; color: {c['muted']}; }}
QToolButton#more:hover {{ color: {c['text']}; }}
QToolButton#more::menu-indicator {{ image: none; width: 0; }}
QTableWidget {{ background: {c['card']}; border: none; selection-background-color: {c['sel']};
               selection-color: {c['text']}; outline: 0; }}
QHeaderView {{ background: {c['card']}; border: none; }}
QTableCornerButton::section {{ background: {c['card']}; border: none; }}
QHeaderView::section {{ background: {c['card']}; color: {c['muted']}; border: none;
                       border-bottom: 1px solid {c['border']}; padding: 4px 8px; font-size: 9pt; }}
QSplitter::handle {{ background: transparent; }}
QMenu {{ background: {c['card']}; border: 1px solid {c['border']}; padding: 4px; }}
QMenu::item {{ padding: 6px 22px 6px 26px; border-radius: 4px; }}
QMenu::item:selected {{ background: {c['sel']}; }}
QMenu::item:disabled {{ color: {c['muted']}; }}
QMenu::separator {{ height: 1px; background: {c['border']}; margin: 4px 8px; }}
QToolTip {{ background: {c['card']}; color: {c['text']}; border: 1px solid {c['border']}; padding: 6px; }}
QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; }}
QScrollBar::handle {{ background: {c['border']}; border-radius: 5px; min-height: 30px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
"""


# --------------------------------------------------------------------------------------------- small widgets
class Bars(QtWidgets.QWidget):
    """Five rising bars, like a phone's signal meter."""

    def __init__(self, off):
        super().__init__()
        self.setFixedSize(30, 20)
        self.n, self.color, self.off = 0, off, off

    def set(self, n, color):
        if (n, color) != (self.n, self.color):
            self.n, self.color = n, color
            self.update()

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        w, gap, h = 4, 2, self.height()
        for i in range(5):
            bh = h * (i + 1) / 5
            p.setBrush(QtGui.QColor(self.color if i < self.n else self.off))
            p.drawRoundedRect(QtCore.QRectF(i * (w + gap), h - bh, w, bh), 1, 1)
        p.end()


class Tile(QtWidgets.QFrame):
    """A title, a big value (optionally with bars) and an optional caption."""

    def __init__(self, title, tooltip, c, bars=False):
        super().__init__()
        named(self, "card")
        self.c, self.key = c, None
        self.setToolTip(tip(tooltip))
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(14, 8, 14, 8)
        lay.setSpacing(0)
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)
        self.bars = Bars(c["border"]) if bars else None
        if self.bars:
            row.addWidget(self.bars, 0, Qt.AlignmentFlag.AlignVCenter)
        self.v = named(QtWidgets.QLabel("–"), "tileValue")
        row.addWidget(self.v)
        row.addStretch(1)
        self.cap = named(QtWidgets.QLabel(""), "hint")
        self.cap.hide()
        self.title = named(QtWidgets.QLabel(title), "tileTitle")
        lay.addWidget(self.title)
        lay.addLayout(row)
        lay.addWidget(self.cap)

    def set_title(self, title, tooltip):
        if self.title.text() != title:
            self.title.setText(title)
            self.setToolTip(tip(tooltip))

    def set(self, text, color=None, bars=0, bar_color=None, caption=None):
        key = (text, color, bars, bar_color, caption)
        if key == self.key:
            return
        self.key = key
        self.v.setText(text)
        self.v.setStyleSheet(f"color: {color};" if color else "")
        if self.bars:
            self.bars.set(bars, bar_color or self.c["muted"])
        self.cap.setText(caption or "")
        self.cap.setVisible(bool(caption))


class StatusCard(QtWidgets.QFrame):
    """The one-line answer to 'is my link working?', coloured by meaning, with a fix button when there is one."""
    GLYPH = {"ok": "✓", "warn": "!", "bad": "✕", "idle": "•"}

    def __init__(self, c):
        super().__init__()
        named(self, "status")
        self.c, self.key = c, None
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(12)
        self.icon = QtWidgets.QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self.icon.setFixedSize(30, 30)
        col = QtWidgets.QVBoxLayout()
        col.setSpacing(2)
        self.head = named(QtWidgets.QLabel(), "headline")
        self.detail = named(QtWidgets.QLabel(), "muted")
        self.detail.setWordWrap(True)
        self.tech = named(QtWidgets.QLabel(), "hint")
        self.tech.setWordWrap(True)
        self.tech.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.tech.hide()
        for w in (self.head, self.detail, self.tech):
            col.addWidget(w)
        self.more = named(QtWidgets.QPushButton("Technical details"), "link")
        self.more.setCheckable(True)
        self.more.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.more.toggled.connect(self.tech.setVisible)
        self.more.hide()
        self.link = named(QtWidgets.QPushButton(), "link")
        self.link.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.link.hide()
        self.fix = named(QtWidgets.QPushButton(), "primary")
        self.fix.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.fix.hide()
        lay.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(col, 1)
        lay.addWidget(self.link, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self.more, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self.fix, 0, Qt.AlignmentFlag.AlignVCenter)

    def set(self, st, tech=None, link=None):
        key = (st, tech, link)
        if key == self.key:
            return
        self.key = key
        color = self.c[st.level]
        self.setStyleSheet(f"QFrame#status {{ background: {mix(self.c['card'], color, 0.10)}; "
                           f"border: 1px solid {mix(self.c['border'], color, 0.35)}; "
                           f"border-left: 4px solid {color}; border-radius: 8px; }}")
        self.icon.setText(self.GLYPH[st.level])
        self.icon.setStyleSheet(f"background: {color}; color: #ffffff; border-radius: 15px; "
                                f"font-weight: 700; font-size: 13pt;")
        self.head.setText(st.headline)
        self.detail.setText(st.detail)
        self.fix.setText({"lower": "Lower sensitivity", "raise": "Raise sensitivity",
                          "cancel": "Cancel"}.get(st.action, ""))
        self.fix.setToolTip(tip("Stop trying, and keep the sensitivity you had." if st.action == "cancel" else
                                "Changes the sensitivity by one step. The radio restarts, so about a second of "
                                "packets is missed."))
        self.fix.setVisible(bool(st.action))
        self.link.setText(link or "")
        self.link.setVisible(bool(link))
        self.tech.setText(tech or "")
        self.more.setVisible(bool(tech))
        if not tech:
            self.more.setChecked(False)


def card(title, hint="", right=None):
    """A titled card. Returns (frame, body layout)."""
    f = named(QtWidgets.QFrame(), "card")
    v = QtWidgets.QVBoxLayout(f)
    v.setContentsMargins(12, 8, 12, 8)
    v.setSpacing(4)
    head = QtWidgets.QHBoxLayout()
    head.setSpacing(10)
    head.addWidget(named(QtWidgets.QLabel(title), "section"))
    head.addWidget(named(QtWidgets.QLabel(hint), "hint"))
    head.addStretch(1)
    if right is not None:
        head.addWidget(right)
    v.addLayout(head)
    return f, v


def plot(c, interactive=False):
    """A quiet plot: no zoom, menu or auto-range button unless asked (beginners get lost in a zoomed plot)."""
    w = pg.PlotWidget()
    pi = w.getPlotItem()
    pi.hideButtons()
    pi.setMenuEnabled(interactive)
    w.setMouseEnabled(x=interactive, y=interactive)
    for ax in ("left", "bottom"):
        w.getAxis(ax).setPen(c["border"])
        w.getAxis(ax).setTextPen(c["muted"])
    return w


# --------------------------------------------------------------------------------------------- window
class Main(QtWidgets.QMainWindow):
    def __init__(self, args, c):
        super().__init__()
        self.args, self.c = args, c
        self.kind = {"ok": ("✓ OK", c["ok"]), "bad": ("✗ Damaged", c["bad"]), "other": ("• Other signal", c["other"])}
        self.brush = {k: pg.mkBrush(v[1]) for k, v in self.kind.items()}
        if args.settings:                             # tests: their own file, never the user's saved choices
            self.settings = QtCore.QSettings(args.settings, QtCore.QSettings.Format.IniFormat)
            self.settings.clear()
        else:
            self.settings = QtCore.QSettings("air-monitor", "air-monitor")
        self.alerts_on = self.settings.value("alerts", not args.screenshot, type=bool)
        self.mhz = f"{am.CH / 1e6:g} MHz"
        self.row_s = am.BLOCK / am.FS                # one waterfall row per read (50 ms)
        self.setWindowTitle(f"Air Monitor · {self.mhz} LoRa")
        self.q = mp.Queue(maxsize=400)
        self.worker = None
        self.csv = self.csv_key = None
        self.source = "demo" if args.demo else "file" if args.file else "hackrf"
        self.file = args.file
        self.file_fc = args.file_fc * 1e6 if args.file and args.file_fc else am.FC
        self.error, self.finished, self.worker_msg = None, False, ""
        self.rec_path = self.rec_t0 = None
        self.resize_due, self.last_resize = False, 0.0
        self.detail_widgets = []
        self.session = self.last_session = None     # this listening session, and the last one that ended
        self.alert = lh.LinkAlert()
        self.waiting = False                        # waiting for the HackRF to come back
        self.fb = None                              # "Find best" in progress
        self.notice = None                          # (Status, until): a result shown for a few seconds
        self.last_hist = 0.0
        self.retry_proc = QtCore.QProcess(self)
        self.retry_proc.finished.connect(self.hackrf_checked)
        self.retry_timer = QtCore.QTimer(self)
        self.retry_timer.setInterval(2000)
        self.retry_timer.timeout.connect(self.check_hackrf)
        self.reset_stats()
        self.reset_run()

        central = named(QtWidgets.QWidget(), "root")
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(10)
        root.addLayout(self.build_header())
        self.status_card = StatusCard(c)
        self.status_card.fix.clicked.connect(self.fix_it)
        self.status_card.link.clicked.connect(self.show_summary)
        root.addWidget(self.status_card)
        self.stack = QtWidgets.QStackedWidget()
        self.stack.addWidget(self.build_welcome())
        live = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(live)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(10)
        lv.addLayout(self.build_tiles())
        lv.addWidget(self.build_views(), 1)
        self.stack.addWidget(live)
        root.addWidget(self.stack, 1)
        root.addLayout(self.build_footer())

        # gains: the command line wins, else the last ones used
        if args.lna is not None or args.vga is not None:
            lna, vga = args.lna if args.lna is not None else 16, args.vga if args.vga is not None else 20
        else:
            lna = self.settings.value("lna", 16, type=int)
            vga = self.settings.value("vga", 20, type=int)
        self.set_gain(lna, vga, save=False, restart=False)
        details = args.details if args.details is not None else self.settings.value("details", False, type=bool)
        self.set_details(details, save=args.details is None)

        geo = self.settings.value("geometry")
        if args.size:
            self.resize(*(int(v) for v in args.size.lower().split("x")))
        elif geo is None or not self.restoreGeometry(geo):
            self.resize(1280, 920)
        self.setMinimumSize(960, 680)

        QtGui.QShortcut(QtGui.QKeySequence(Qt.Key.Key_Space), self, activated=self.toggle)
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(50)
        self.update_subtitle()
        self.update_controls()
        self.refresh()

    # ----- layout
    def build_header(self):
        bar = QtWidgets.QHBoxLayout()
        bar.setSpacing(10)
        col = QtWidgets.QVBoxLayout()
        col.setSpacing(0)
        col.addWidget(named(QtWidgets.QLabel("Air Monitor"), "title"))
        self.subtitle = named(QtWidgets.QLabel(), "muted")
        col.addWidget(self.subtitle)
        bar.addLayout(col)
        bar.addStretch(1)
        self.rec_pill = named(QtWidgets.QLabel(), "rec")
        self.rec_pill.hide()
        bar.addWidget(self.rec_pill)
        self.btn = named(QtWidgets.QPushButton("▶  Start"), "primary")
        self.btn.setMinimumWidth(110)
        self.btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn.setToolTip("Start or stop listening (Space)")
        self.btn.clicked.connect(self.toggle)
        bar.addWidget(self.btn)
        more = named(QtWidgets.QToolButton(), "more")
        more.setText("⋯")
        more.setToolTip("More: demo, recordings, logs folder, help")
        more.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        more.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        more.setMenu(self.build_menu())
        bar.addWidget(more)
        return bar

    def build_menu(self):
        m = QtWidgets.QMenu(self)
        group = QtGui.QActionGroup(self)
        self.act_src = {}
        for key, text in (("hackrf", "Listen live (HackRF)"), ("demo", "Play the demo (no HackRF needed)"),
                          ("file", "Play a recording…")):
            a = m.addAction(text)
            a.setCheckable(True)
            group.addAction(a)
            self.act_src[key] = a
        self.act_src["hackrf"].triggered.connect(lambda: self.use_source("hackrf"))
        self.act_src["demo"].triggered.connect(lambda: self.use_source("demo"))
        self.act_src["file"].triggered.connect(self.open_recording)
        m.addSeparator()
        self.act_rec = m.addAction("Record raw radio data")
        self.act_rec.setCheckable(True)
        self.act_rec.toggled.connect(self.record_toggled)
        self.act_alert = m.addAction("Alert me when the link drops")
        self.act_alert.setCheckable(True)
        self.act_alert.setChecked(self.alerts_on)
        self.act_alert.toggled.connect(self.alerts_toggled)
        m.addAction("Open logs folder").triggered.connect(self.open_logs)
        m.addAction("Clear").triggered.connect(self.clear)
        m.addSeparator()
        m.addAction("How to read this screen").triggered.connect(self.show_help)
        return m

    def build_welcome(self):
        page = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(page)
        outer.addStretch(1)
        box = named(QtWidgets.QFrame(), "card")
        box.setMaximumWidth(640)
        v = QtWidgets.QVBoxLayout(box)
        v.setContentsMargins(28, 24, 28, 24)
        v.setSpacing(14)
        v.addWidget(named(QtWidgets.QLabel(f"Watch your {self.mhz} LoRa link"), "headline"))
        intro = named(QtWidgets.QLabel("Air Monitor listens with the HackRF and shows every packet your nodes "
                                       "send, as it happens."), "muted")
        intro.setWordWrap(True)
        v.addWidget(intro)
        steps = ["Plug the HackRF into this computer.",
                 "Keep it at least 1 m from a transmitting node, and close Gqrx or any other radio program.",
                 "Press <b>Start listening</b>."]
        for i, text in enumerate(steps, 1):
            row = QtWidgets.QHBoxLayout()
            row.setSpacing(12)
            num = named(QtWidgets.QLabel(str(i), alignment=Qt.AlignmentFlag.AlignCenter), "stepNum")
            num.setFixedSize(24, 24)
            t = QtWidgets.QLabel(text)
            t.setWordWrap(True)
            row.addWidget(num, 0, Qt.AlignmentFlag.AlignTop)
            row.addWidget(t, 1)
            v.addLayout(row)
        btns = QtWidgets.QHBoxLayout()
        go = named(QtWidgets.QPushButton("▶  Start listening"), "primary")
        go.clicked.connect(lambda: self.use_source("hackrf"))
        demo = QtWidgets.QPushButton("Try the demo")
        demo.setToolTip(tip("Plays an example recording, so you can learn the screen without a HackRF."))
        demo.clicked.connect(lambda: self.use_source("demo"))
        for b in (go, demo):
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btns.addWidget(b)
        btns.addStretch(1)
        v.addSpacing(4)
        v.addLayout(btns)
        foot = named(QtWidgets.QLabel("No HackRF? The demo plays an example recording. Every packet heard is "
                                      "also saved in the logs folder (⋯ menu)."), "hint")
        foot.setWordWrap(True)
        v.addWidget(foot)
        row = QtWidgets.QHBoxLayout()
        row.addStretch(1)
        row.addWidget(box, 3)
        row.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(2)
        return page

    def build_tiles(self):
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(10)
        self.tiles = {}
        spec = [
            ("ok", "Received", "LoRa packets read correctly since Start (or since Clear).", False, 1),
            ("bad", "Damaged", "LoRa packets that were heard but failed their check (CRC), so their content "
                               "can't be trusted.", False, 1),
            ("signal", "Signal", "How loud your nodes are at the HackRF: the loudest packet of the last 5 s "
                                 "(level in dBFS: 0 dBFS is the most it can take). <b>Too strong</b> means it is "
                                 "overloaded: lower the sensitivity or move the HackRF further away.", True, 1),
            ("quality", "Quality", "How clearly packets stand out from the noise: the typical (median) SNR of the "
                                   "last 5 s. LoRa still reads packets a little below 0 dB, so Fair is fine.",
             True, 1),
            ("rate", "Packets / s", "Packets read correctly per second, over the last 5 s.", False, 0),
            ("other", "Other signals", "Bursts on the channel that are not LoRa: Wi-Fi, Bluetooth, microwave "
                                       "ovens… (since Start).", False, 0),
            ("interval", "Interval", "Time between the last two LoRa packets.", False, 0),
            ("cfo", "Freq error", f"How far the last packet's carrier was from {am.CH / 1e6:.1f} MHz.", False, 0),
        ]
        for key, title, tooltip, bars, simple in spec:
            self.tiles[key] = Tile(title, tooltip, self.c, bars)
            row.addWidget(self.tiles[key], 1)
            if not simple:
                self.detail_widgets.append(self.tiles[key])
        return row

    def build_views(self):
        c = self.c
        split = QtWidgets.QSplitter(Qt.Orientation.Vertical)
        split.setChildrenCollapsible(False)
        split.setHandleWidth(10)
        f_mhz = (am.FC + (np.arange(am.NFFT) - am.NFFT // 2) * am.FS / am.NFFT) / 1e6
        self.f_mhz = f_mhz
        lo_ch, hi_ch = (am.CH - am.BW / 2) / 1e6, (am.CH + am.BW / 2) / 1e6

        # spectrum (details)
        self.spec = plot(c, interactive=True)
        self.spec.showGrid(x=True, y=True, alpha=0.15)
        self.spec.setYRange(-110, -10)
        self.spec.getAxis("left").setTicks([[(-20, "-20"), (-60, "-60"), (-100, "-100")]])
        self.spec.setXRange(f_mhz[0], f_mhz[-1], padding=0)
        self.spec.addItem(pg.LinearRegionItem((lo_ch, hi_ch), movable=False, brush=pg.mkBrush(46, 157, 79, 40),
                                              pen=pg.mkPen(None)))
        self.c_max = self.spec.plot(pen=pg.mkPen("#e0a030", width=1))
        self.c_mean = self.spec.plot(pen=pg.mkPen("#3a8fd8", width=1))
        self.peak = None
        f, v = card("Spectrum", "now (blue) and peak hold (orange) · dBFS · MHz")
        v.addWidget(self.spec, 1)
        self.detail_widgets.append(f)
        split.addWidget(f)

        # waterfall: the main picture
        self.wf = np.full((WF_ROWS, am.NFFT), np.nan, np.float32)        # NaN: no data yet (drawn clear)
        wfw = plot(c)
        self.img = pg.ImageItem()
        self.img.setColorMap(pg.colormap.get("viridis"))
        self.wf_rect = QtCore.QRectF(f_mhz[0], 0, f_mhz[-1] - f_mhz[0], WF_ROWS)
        wfw.addItem(self.img)
        wfw.setXRange(f_mhz[0], f_mhz[-1], padding=0)
        wfw.setYRange(0, WF_ROWS, padding=0)
        wfw.getAxis("left").setTicks([[(WF_ROWS - 8, "now"), (WF_ROWS / 2, "5 s ago"), (12, "10 s ago")]])
        wfw.getAxis("left").setWidth(56)
        wfw.setMinimumHeight(115)                     # the main picture: the other cards give way first
        edge = pg.mkPen(255, 255, 255, 170, width=1, style=Qt.PenStyle.DashLine)
        for x in (lo_ch, hi_ch):
            wfw.addItem(pg.InfiniteLine(x, angle=90, pen=edge, movable=False))
        label = pg.TextItem("LoRa channel", color=(255, 255, 255), anchor=(0.5, 0), fill=pg.mkBrush(0, 0, 0, 120))
        label.setPos(am.CH / 1e6, WF_ROWS)
        wfw.addItem(label)
        # packet lane: a thin column right of the channel, one coloured tick at the row where each packet was heard
        self.lane = pg.ImageItem()
        self.lane.setZValue(10)
        wfw.addItem(self.lane)
        self.lane_rect = QtCore.QRectF(hi_ch + 0.07, 0, 0.14, WF_ROWS)
        self.lane_rgba = {k: QtGui.QColor(v[1]).getRgb() for k, v in self.kind.items()}
        wfw.setXLink(self.spec)
        legend = named(QtWidgets.QLabel(
            f"<span style='color:{c['ok']}'>●</span> read OK &nbsp;&nbsp;"
            f"<span style='color:{c['bad']}'>●</span> damaged &nbsp;&nbsp;"
            f"<span style='color:{c['other']}'>●</span> other signal"), "hint")
        f, v = card("What's on the air", "last 10 s · newest at the top · brighter = stronger · MHz", legend)
        f.setToolTip(tip("A waterfall: each row is 50 ms of radio, newest at the top. Brighter means a stronger "
                         "signal. The dashed lines mark your LoRa channel; each packet is a bright bar between "
                         "them, with a tick to its right showing whether it was read."))
        v.addWidget(wfw, 1)
        split.addWidget(f)

        # packet strip: channel power over the last 2 s
        self.tl = plot(c)
        self.tl.showGrid(x=True, y=False, alpha=0.12)
        self.tl.setXRange(-TL_S - 0.12, 0.06, padding=0)
        self.tl.setYRange(-80, -10)
        self.tl.getAxis("bottom").setTicks([[(-2, "2 s ago"), (-1.5, ""), (-1, "1 s ago"), (-0.5, ""),
                                             (0, "now")]])
        self.tl.getAxis("left").setWidth(56)
        self.tl.setMinimumHeight(36)
        self.tl.setLabel("left", "dBFS")
        self.tl.getAxis("left").setTicks([[(-20, "-20"), (-70, "-70")]])     # few labels: the card is short
        self.c_tl = self.tl.plot(pen=pg.mkPen("#3a8fd8", width=1))
        self.thr_line = pg.InfiniteLine(angle=0, pen=pg.mkPen("#e0a030", style=Qt.PenStyle.DashLine))
        self.tl.addItem(self.thr_line)
        self.marks = pg.ScatterPlotItem(size=9, pen=None)
        self.tl.addItem(self.marks)
        f, v = card("Packets", "last 2 s · each spike is one transmission")
        f.setToolTip(tip("Radio power on your LoRa channel over the last 2 seconds. Every transmission is a "
                         "spike; the dot on top shows what it was. Evenly spaced spikes are the nodes taking "
                         "turns (their time slots)."))
        v.addWidget(self.tl, 1)
        split.addWidget(f)

        # messages
        t = self.table = QtWidgets.QTableWidget(0, len(COLS))
        t.setHorizontalHeaderLabels(COLS)
        heads = {1: "OK: read correctly. Damaged: heard, but failed its check (CRC).",
                 2: "How clearly the packet stood out from the noise.", 3: "What the node sent: its text, or its bytes in hex if it isn't text.",
                 4: "Time since the previous LoRa packet.", 5: "Payload length in bytes.",
                 6: "Header and payload checksums.", 7: "Level at the HackRF (0 dBFS = full scale).",
                 8: "Signal-to-noise ratio in the channel.", 9: "Carrier frequency error.",
                 10: "Burst length (only for bursts well above the noise)."}
        for i, text in heads.items():
            t.horizontalHeaderItem(i).setToolTip(tip(text))
        t.verticalHeader().setVisible(False)
        t.verticalHeader().setDefaultSectionSize(26)
        t.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        t.setShowGrid(False)
        t.setWordWrap(False)
        t.horizontalHeader().setStretchLastSection(True)
        t.horizontalHeader().setHighlightSections(False)
        t.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.mono = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont)
        # history: packets per second over the last 10 minutes
        self.hist = plot(c)
        self.hist.setMinimumHeight(32)
        self.hist.showGrid(x=True, y=False, alpha=0.12)
        self.hist.setXRange(-10.5, 0.4, padding=0)    # room for the edge labels
        self.hist.setYRange(0, 1)
        self.hist.getAxis("bottom").setTicks([[(-10, "10 min ago"), (-7.5, ""), (-5, "5 min ago"), (-2.5, ""),
                                               (0, "now")]])
        self.hist.getAxis("left").setWidth(56)
        self.hist.getAxis("left").setStyle(showValues=False)    # the shape matters; the rate is in the status
        self.c_hist = self.hist.plot(pen=pg.mkPen(c["accent"], width=1.5))
        self.hist_lost = pg.ScatterPlotItem(size=7, pen=None, brush=pg.mkBrush(c["bad"]))
        self.hist.addItem(self.hist_lost)
        f, v = card("History", "last 10 min · packets per second · red = packets lost")
        f.setToolTip(tip("How the link has been doing: packets read per second, in 5 s steps. A drop to zero "
                         "means nothing was heard; red dots mark moments when packets were lost."))
        v.addWidget(self.hist, 1)
        split.addWidget(f)

        f, v = card("Messages", "newest first")
        v.addWidget(t, 1)
        split.addWidget(f)
        t.setMinimumHeight(48)
        split.setSizes([180, 360, 130, 100, 230])
        for i, s in enumerate((2, 4, 2, 1, 3)):
            split.setStretchFactor(i, s)
        return split

    def build_footer(self):
        bar = QtWidgets.QHBoxLayout()
        bar.setSpacing(8)
        lab = named(QtWidgets.QLabel("Sensitivity"), "muted")
        lab.setToolTip(tip("How much the HackRF amplifies what it hears. <b>Normal</b> suits most cases. Use "
                           "<b>Low</b> if the status says the signal is too strong, <b>High</b> if it is weak. "
                           "Changing it restarts the radio for about a second."))
        bar.addWidget(lab)
        seg = QtWidgets.QHBoxLayout()
        seg.setSpacing(0)
        self.sens = QtWidgets.QButtonGroup(self)
        for i, name in enumerate(lh.ORDER):
            b = named(QtWidgets.QPushButton(name), "seg")
            b.setProperty("pos", "first" if i == 0 else "last" if i == len(lh.ORDER) - 1 else "mid")
            b.setCheckable(True)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            lna, vga = lh.SENSITIVITY[name]
            b.setToolTip(f"LNA {lna} dB, VGA {vga} dB")
            self.sens.addButton(b)
            seg.addWidget(b)
        self.sens.buttonClicked.connect(lambda b: self.set_gain(*lh.SENSITIVITY[b.text()]))
        bar.addLayout(seg)
        self.find_btn = QtWidgets.QPushButton("Find best")
        self.find_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.find_btn.setToolTip(tip("Tries Low, Normal and High for a few seconds each, and keeps the one that "
                                     "loses the fewest packets without overloading the HackRF. Takes about 15 s; "
                                     "a node must be transmitting."))
        self.find_btn.clicked.connect(self.find_best)
        bar.addWidget(self.find_btn)
        self.custom_lbl = named(QtWidgets.QLabel(), "hint")
        bar.addWidget(self.custom_lbl)

        raw = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(raw)
        h.setContentsMargins(12, 0, 0, 0)
        h.setSpacing(6)
        self.lna = QtWidgets.QComboBox()
        self.lna.addItems([str(v) for v in LNA_STEPS])
        self.vga = QtWidgets.QSpinBox()
        self.vga.setRange(0, 62)
        self.vga.setSingleStep(2)
        self.lna.currentTextChanged.connect(self.gain_widgets_changed)
        self.vga.valueChanged.connect(self.gain_widgets_changed)
        for w in (named(QtWidgets.QLabel("LNA dB"), "muted"), self.lna, named(QtWidgets.QLabel("VGA dB"), "muted"),
                  self.vga):
            h.addWidget(w)
        self.detail_widgets.append(raw)
        bar.addWidget(raw)
        bar.addStretch(1)
        radio = named(QtWidgets.QLabel(f"Radio: {am.RADIO.label()}"), "hint")
        radio.setToolTip(tip("The nodes' radio settings. Change them in air-monitor.toml (see the README)."))
        self.detail_widgets.append(radio)
        bar.addWidget(radio)
        self.health = named(QtWidgets.QLabel(), "hint")
        self.health.setToolTip(tip("Receiver health: input sample rate, 50 ms blocks dropped because the "
                                   "computer fell behind, and data waiting to be processed."))
        self.detail_widgets.append(self.health)
        bar.addWidget(self.health)
        self.details_btn = named(QtWidgets.QPushButton("Show details"), "link")
        self.details_btn.setCheckable(True)
        self.details_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.details_btn.setToolTip(tip("Spectrum, raw gains, technical columns and receiver health."))
        self.details_btn.toggled.connect(self.set_details)
        bar.addWidget(self.details_btn)
        return bar

    # ----- state
    def reset_stats(self):
        self.count = {"ok": 0, "bad": 0, "other": 0}
        self.seqs = payloads.SequenceTracker()      # lost packets, from the nodes' packet counters

    def reset_run(self):
        """State that belongs to one run of the receiver (its stream time starts at 0)."""
        self.recent = collections.deque()           # (stream_s, kind, level_db, snr_db), last WINDOW_S
        self.now = 0.0
        self.have_data = False
        self.tl_t, self.tl_db = collections.deque(), collections.deque()
        self.mark_pts = []
        self.last_lora = None
        self.ever_lora = False
        self.rate, self.dropped, self.backlog = None, 0, 0
        self.drop_seen = -1e9
        self.run_t0 = time.time()
        self.seqs.new_run()                         # don't count the packets missed while restarting as lost
        self.alert.reset()                          # ... nor take the restart for a dropped link
        if self.session:
            self.session.new_run()
        if hasattr(self, "marks"):
            self.marks.clear()
            self.c_tl.setData([], [])

    def clear(self):
        self.reset_stats()
        self.table.setRowCount(0)
        self.peak = None
        self.recent.clear()
        self.mark_pts = []
        self.marks.clear()
        self.lane.clear()
        self.last_lora = None
        for t in self.tiles.values():
            t.set("–")
        if self.session:                            # "since Clear": the session starts again too
            self.session = Session(self.source_label(), time.time())
        self.c_hist.setData([], [])
        self.hist_lost.clear()
        self.refresh()

    def gains(self):
        return int(self.lna.currentText()), self.vga.value()

    def set_gain(self, lna, vga, save=True, restart=True):
        for w in (self.lna, self.vga):
            w.blockSignals(True)
        self.lna.setCurrentText(str(lna))
        self.vga.setValue(vga)
        for w in (self.lna, self.vga):
            w.blockSignals(False)
        name = lh.preset_name(*self.gains())
        self.sens.setExclusive(False)
        for b in self.sens.buttons():
            b.setChecked(b.text() == name)
        self.sens.setExclusive(True)
        self.update_controls()
        if save:
            self.settings.setValue("lna", lna)
            self.settings.setValue("vga", vga)
        if restart and self.worker and self.source == "hackrf":
            self.restart()

    def gain_widgets_changed(self, *_):
        self.set_gain(*self.gains())

    def fix_it(self):
        st = self.status
        if st.action == "cancel":
            self.cancel_find_best()
            return
        name = lh.step_sensitivity(*self.gains(), -1 if st.action == "lower" else +1) if st.action else None
        if name:
            self.set_gain(*lh.SENSITIVITY[name])

    def set_details(self, on, save=True):
        self.details = bool(on)
        for w in self.detail_widgets:
            w.setVisible(self.details)
        for i in range(SIMPLE_COLS, len(COLS)):
            self.table.setColumnHidden(i, not self.details)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(SIMPLE_COLS - 1, QtWidgets.QHeaderView.ResizeMode.Interactive if self.details
                                else QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.tl.getPlotItem().showAxis("left", self.details)
        self.thr_line.setVisible(self.details)
        self.details_btn.blockSignals(True)
        self.details_btn.setChecked(self.details)
        self.details_btn.blockSignals(False)
        self.details_btn.setText("Hide details" if self.details else "Show details")
        if save:
            self.settings.setValue("details", self.details)
        self.resize_due = True
        self.tile_refresh()

    def source_label(self):
        return {"hackrf": "live from the HackRF", "demo": "demo recording (no HackRF needed)",
                "file": f"recording {os.path.basename(self.file or '')}"}[self.source]

    def update_subtitle(self):
        self.subtitle.setText(f"{self.mhz} · LoRa · {self.source_label()}")
        self.subtitle.setToolTip(self.worker_msg)
        for key, a in self.act_src.items():
            a.setChecked(key == self.source)

    def update_controls(self):
        live = self.source == "hackrf"
        for w in self.sens.buttons() + [self.lna, self.vga]:
            w.setEnabled(live and self.fb is None)
        self.find_btn.setEnabled(live and self.fb is None)
        name = lh.preset_name(*self.gains())
        self.custom_lbl.setText("only for the live HackRF" if not live else
                                "" if name else "Custom (LNA {}, VGA {})".format(*self.gains()))
        self.act_rec.setEnabled(live and self.worker is not None)
        busy = self.worker is not None or self.waiting or self.fb is not None
        self.btn.setText("■  Stop" if busy else "▶  Start")

    # ----- sources and running
    def use_source(self, source, path=None):
        self.stop(end_session=True)
        self.source = source
        if path:
            self.file, self.file_fc = path, am.FC
        self.update_subtitle()
        self.start()

    def open_recording(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Play a recording", am.LOGS,
                                                        "Radio recordings (*.iq);;All files (*)")
        if path:
            self.use_source("file", path)
        else:
            self.update_subtitle()                     # put the tick back on the current source

    def toggle(self):
        if self.worker or self.waiting or self.fb:
            self.stop(end_session=True)
        else:
            self.start()

    def restart(self):
        """Restart the receiver (e.g. new gains) in the same session."""
        self.stop()
        self.start(new_session=False)

    def start(self, new_session=True):
        if self.worker:
            return
        self.error, self.finished, self.worker_msg = None, False, ""
        self.stack.setCurrentIndex(1)
        if new_session or self.session is None:
            self.begin_session()
        if self.source == "hackrf" and shutil.which("hackrf_transfer") is None:
            self.error = "hackrf_transfer: not installed (not found on the PATH)"
            self.refresh()
            return
        self.reset_run()
        self.q = mp.Queue(maxsize=400)                # fresh queue per run
        self.worker = am.Worker(self.q, *self.gains(), file=self.file if self.source == "file" else None,
                                file_fc=self.file_fc, realtime=not self.args.fast, demo=self.source == "demo")
        self.worker.start()
        self.update_subtitle()
        self.update_controls()
        self.refresh()

    def stop(self, end_session=False):
        """Stop the receiver. end_session: the user is done (Stop, another source, end of a recording, closing):
        also stop waiting or searching, and wrap up the session summary."""
        if self.rec_path:
            self.set_record(False)
        if self.worker:
            self.worker.stop()
            self.worker = None
        if end_session:
            self.stop_waiting()
            if self.fb:
                self.cancel_find_best(restart=False)
            self.finish_session()
        self.update_controls()
        self.refresh()

    # ----- sessions
    def begin_session(self):
        self.finish_session()
        self.reset_stats()
        self.session = Session(self.source_label(), time.time())
        self.last_session = None
        self.c_hist.setData([], [])
        self.hist_lost.clear()

    def finish_session(self):
        """End the session: keep it for the status line and 'View summary', and save the summary beside the
        packet log (not for the demo)."""
        s, self.session = self.session, None
        if s is None or not s.active:
            return
        s.end = time.time()
        if self.csv and self.csv_key and self.csv_key[0] == self.source:
            s.log_path = os.path.relpath(self.csv_path, os.path.dirname(am.LOGS))
        s.summary_path = None
        if self.source != "demo":
            path = os.path.join(am.LOGS, f"air_{datetime.datetime.fromtimestamp(s.start):%Y%m%d_%H%M%S}"
                                         f".summary.txt")
            try:
                os.makedirs(am.LOGS, exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(s.summary_text(s.end))
                s.summary_path = path
            except OSError:
                pass
        self.last_session = s

    def show_summary(self):
        s = self.last_session
        if s is None:
            return
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Session summary")
        box.setTextFormat(Qt.TextFormat.RichText)
        rows = "".join(f"<tr><td style='padding-right:16px; color:{self.c['muted']}'>{html.escape(k)}</td>"
                       f"<td>{html.escape(v)}</td></tr>" for k, v in s.summary(s.end))
        box.setText(f"<table>{rows}</table>")
        folder = None
        if s.summary_path:
            box.setInformativeText(f"Saved as {os.path.relpath(s.summary_path, os.path.dirname(am.LOGS))}")
            folder = box.addButton("Open logs folder", QtWidgets.QMessageBox.ButtonRole.ActionRole)
        box.addButton(QtWidgets.QMessageBox.StandardButton.Close)
        box.exec()
        if folder is not None and box.clickedButton() is folder:
            self.open_logs()

    def update_history(self, now):
        h = self.session.history(now) if self.session else []
        self.last_hist = now
        if not h:
            return
        x, y = [p[0] for p in h], [p[1] for p in h]
        self.c_hist.setData(x, y)
        self.hist_lost.setData([p[0] for p in h if p[2]], [p[1] for p in h if p[2]])
        self.hist.setYRange(0, max(1.0, max(y) * 1.25), padding=0)

    def gain_label(self):
        """The sensitivity, as the session summary lists it (live HackRF only)."""
        if self.source != "hackrf":
            return ""
        lna, vga = self.gains()
        return f"{lh.preset_name(lna, vga) or 'Custom'} (LNA {lna}, VGA {vga})"

    def alerts_toggled(self, on):
        self.alerts_on = on
        self.settings.setValue("alerts", on)

    # ----- waiting for the HackRF to come back
    def on_error(self, raw):
        """The receiver stopped with an error. If the HackRF can come back by itself (unplugged, busy, USB
        hiccup), wait for it and carry on in the same session; else end the session."""
        self.stop()
        self.error = raw
        if self.source == "hackrf" and lh.should_retry(raw):
            if self.fb:                                # can't finish the search: put the old gains back
                fb, self.fb = self.fb, None
                self.set_gain(*fb["prev"], save=False, restart=False)
            if self.alerts_on and self.alert.state == "up":
                notify(self, "The HackRF stopped", "Listening starts again by itself when it's back.")
            self.alert.reset()
            self.waiting = True
            self.retry_timer.start()
            self.update_controls()
        else:
            self.stop(end_session=True)

    def stop_waiting(self):
        self.waiting = False
        self.retry_timer.stop()
        if self.retry_proc.state() != QtCore.QProcess.ProcessState.NotRunning:
            self.retry_proc.kill()

    def check_hackrf(self):
        if self.worker is None and self.retry_proc.state() == QtCore.QProcess.ProcessState.NotRunning:
            self.retry_proc.start("hackrf_info", [])

    def hackrf_checked(self, *_):
        out = (bytes(self.retry_proc.readAllStandardOutput()) +
               bytes(self.retry_proc.readAllStandardError())).decode(errors="replace")
        if self.waiting and "Found HackRF" in out and "busy" not in out.lower():
            self.stop_waiting()
            self.start(new_session=False)

    # ----- "Find best": try each sensitivity for a few seconds, keep the best
    def find_best(self):
        if self.source != "hackrf" or self.fb or self.waiting:
            return
        if not self.worker:
            self.start()
        self.notice = None
        self.fb = dict(names=list(lh.ORDER), i=-1, prev=self.gains(), trials={}, phase=None, t=0.0, cur=None)
        self.fb_next()

    def fb_next(self):
        fb = self.fb
        if fb["cur"] is not None:
            c = fb["cur"]
            fb["trials"][fb["names"][fb["i"]]] = dict(
                ok=c["ok"], bad=c["bad"], not_heard=c["not_heard"], peak=max(c["levels"], default=None),
                snr=statistics.median(c["snrs"]) if c["snrs"] else None)
        fb["i"] += 1
        if fb["i"] == len(fb["names"]):
            self.fb_finish()
            return
        fb["cur"] = dict(ok=0, bad=0, not_heard=0, levels=[], snrs=[])
        fb["phase"], fb["t"] = "settle", time.time()
        self.set_gain(*lh.SENSITIVITY[fb["names"][fb["i"]]], save=False)     # restarts the receiver
        self.update_controls()

    def fb_tick(self):
        fb, now = self.fb, time.time()
        if fb["phase"] == "settle":
            if self.have_data and now - fb["t"] >= 1.0:
                fb["phase"], fb["t"] = "measure", now
            elif now - fb["t"] > 8.0:                  # no data from the HackRF: counts as nothing heard
                self.fb_next()
        elif fb["phase"] == "measure" and now - fb["t"] >= MEASURE_S:
            self.fb_next()

    def fb_finish(self):
        fb, self.fb = self.fb, None
        name, why = lh.pick_sensitivity(fb["trials"])
        if name:
            self.set_gain(*lh.SENSITIVITY[name])
            self.notice = (lh.Status("ok", f"Sensitivity set to {name}", why, None), time.time() + 15)
        else:
            self.set_gain(*fb["prev"], save=False)
            self.notice = (lh.Status("warn", "Kept the sensitivity as it was", why, None), time.time() + 15)
        self.update_controls()

    def cancel_find_best(self, restart=True):
        fb, self.fb = self.fb, None
        if fb:
            self.set_gain(*fb["prev"], save=False, restart=restart)
        self.update_controls()

    def open_csv(self):
        """One packet log per source, opened at its first packet (the demo is not logged)."""
        key = (self.source, self.file if self.source == "file" else None)
        if self.source == "demo" or key == self.csv_key:
            return
        if self.csv:
            self.csv_f.close()
        os.makedirs(am.LOGS, exist_ok=True)
        path = os.path.join(am.LOGS, f"air_{datetime.datetime.now():%Y%m%d_%H%M%S}.csv")
        self.csv_f = open(path, "w", newline="", buffering=1, encoding="utf-8")
        self.csv = csv.writer(self.csv_f)
        self.csv.writerow(["time", "stream_s", "type", "dt_ms", "dur_ms", "level_dbfs", "snr_db", "freq_err_khz",
                           "len", "header_ok", "crc_ok", "sync", "payload_hex", "text", "seq"])
        self.csv_key, self.csv_path = key, path

    # ----- recording raw IQ
    def record_toggled(self, on):
        if not on:
            self.set_record(False)
            return
        if not (self.worker and self.source == "hackrf"):
            self.set_record(False)
            return
        ans = QtWidgets.QMessageBox.question(
            self, "Record raw radio data",
            "This saves the raw signal from the HackRF, so it can be played back later with "
            "“Play a recording…”.\n\nIt uses about 13 MB of disk every second (almost 800 MB a minute). "
            "Recordings go to the logs folder.\n\nStart recording?")
        if ans != QtWidgets.QMessageBox.StandardButton.Yes or not self.worker:
            self.set_record(False)
            return
        os.makedirs(am.LOGS, exist_ok=True)
        self.rec_path = os.path.join(am.LOGS, f"capture_{datetime.datetime.now():%Y%m%d_%H%M%S}"
                                              f"_{am.FC / 1e6:.1f}MHz_{am.FS / 1e6:.1f}Msps.iq")
        self.rec_t0 = time.time()
        self.worker.set_record(self.rec_path)

    def set_record(self, on):
        """Show recording as on/off; turning it off also tells the receiver."""
        if not on and self.rec_path and self.worker:
            self.worker.set_record(None)
        if not on:
            self.rec_path = None
        self.act_rec.blockSignals(True)
        self.act_rec.setChecked(on)
        self.act_rec.blockSignals(False)

    # ----- other menu actions
    def open_logs(self):
        os.makedirs(am.LOGS, exist_ok=True)
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(am.LOGS))

    def show_help(self):
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("How to read this screen")
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(
            "<p><b>Status</b>: the coloured line at the top says whether the link is working and, if not, "
            "what to do.</p>"
            "<p><b>What's on the air</b>: a waterfall. Each row is 50 ms of radio, newest at the top, and "
            "brighter means stronger. The dashed lines mark your LoRa channel: every packet is a bright bar "
            "between them, with a tick beside it: green = read OK, red = damaged, grey = some other signal "
            "(Wi-Fi, Bluetooth…).</p>"
            "<p><b>Packets</b>: the last 2 seconds of the channel. Each spike is one transmission; evenly "
            "spaced spikes are the nodes taking turns.</p>"
            "<p><b>History</b>: packets per second over the last 10 minutes; red dots mark lost packets.</p>"
            "<p><b>Received / Lost</b>: packets read correctly, and the share that did not arrive intact (worked "
            "out from the nodes' packet numbers; for other nodes this tile shows <b>Damaged</b>: packets that "
            "failed their check). <b>Signal</b>: how loud the nodes are at the HackRF. <b>Quality</b>: how "
            "clearly they stand out from the noise.</p>"
            "<p><b>Messages</b>: what each packet said, newest first (the v0.1 nodes send a packet number).</p>"
            "<p><b>Sensitivity</b>: Normal suits most cases. If the status says the signal is too strong, pick "
            "Low (or press the button in the status line); if it is weak, pick High. <b>Find best</b> tries all "
            "three and keeps the best.</p>"
            "<p>After <b>Stop</b>, the status line sums up the session; <b>View summary</b> shows all the "
            "numbers (also saved next to the packet log). If the HackRF is unplugged, listening starts again "
            "by itself when it is back.</p>"
            "<p><b>Show details</b>: the spectrum, the raw gains and the technical columns, for experts.</p>"
            "<p>Hover over anything to see what it means. Space starts and stops listening.</p>")
        box.exec()

    def closeEvent(self, e):
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop(end_session=True)
        super().closeEvent(e)

    # ----- updates
    def poll(self):
        last = None
        for _ in range(100):
            try:
                kind, val = self.q.get_nowait()
            except queue.Empty:
                break
            if kind == "data":
                self.take(val)
                last = val
            elif kind == "status":
                self.worker_msg = val
                self.update_subtitle()
            elif kind == "done":
                self.stop(end_session=True)
                self.finished = True
            elif kind == "error":
                self.on_error(val)
        if last is not None:
            self.draw(last)
        self.refresh()

    def take(self, out):
        self.have_data = True
        if "spec_mean" in out:
            self.wf = np.roll(self.wf, 1, axis=0)
            self.wf[0] = out["spec_max"]
            self.peak = out["spec_max"] if self.peak is None else np.maximum(self.peak, out["spec_max"])
        if "blk_t" in out:
            self.tl_t.extend(out["blk_t"])
            self.tl_db.extend(out["blk_db"])
            self.now = out["now"]
            while self.tl_t and self.tl_t[0] < self.now - TL_S:
                self.tl_t.popleft()
                self.tl_db.popleft()
        for pk in out["packets"]:
            self.add_packet(pk)
        if "rate" in out:
            if out["dropped"] > self.dropped:
                self.drop_seen = time.time()
            self.rate, self.dropped, self.backlog = out["rate"], out["dropped"], out["backlog"]

    def draw(self, last):
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
        self.mark_pts = [p for p in self.mark_pts if p[0] > self.now - WF_ROWS * self.row_s]
        strip = [p for p in self.mark_pts if p[0] > self.now - TL_S]
        self.marks.setData([p[0] - self.now for p in strip], [p[1] for p in strip],
                           brush=[self.brush[p[2]] for p in strip])
        lane = np.zeros((1, WF_ROWS, 4), np.ubyte)               # [x, y, rgba], y = 0 at the bottom (10 s ago)
        for kind in ("other", "ok", "bad"):                        # damaged last, so it is never hidden
            for t, _, k in self.mark_pts:
                row = WF_ROWS - 1 - int((self.now - t) / self.row_s)
                if k == kind and 0 <= row < WF_ROWS:
                    lane[0, row] = self.lane_rgba[k]
        self.lane.setImage(lane, autoLevels=False)
        self.lane.setRect(self.lane_rect)

    def add_packet(self, pk):
        kind = pk["kind"]
        self.count[kind] += 1
        self.recent.append((pk["stream_s"], kind, pk["level_db"], pk["snr_db"]))
        self.mark_pts.append((pk["stream_s"], pk["level_db"], kind))
        if kind != "other":
            self.last_lora = pk
            self.ever_lora = True
        d = payloads.describe(pk["data"])
        not_heard = 0
        if kind == "ok" and d.seq is not None:
            _, not_heard = self.seqs.good(d.stream, d.seq)
            self.recent.extend((pk["stream_s"], "missed", None, None) for _ in range(not_heard))
        elif kind == "bad":
            self.seqs.bad()
        if self.session:
            self.session.add(pk["wall"], kind, pk["level_db"], pk["snr_db"], self.gain_label(), not_heard,
                             counter=d.seq is not None)
        if self.fb and self.fb["phase"] == "measure" and kind != "other":
            cur = self.fb["cur"]
            cur[kind] += 1
            cur["not_heard"] += not_heard
            cur["levels"].append(pk["level_db"])
            cur["snrs"].append(pk["snr_db"])
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in pk["data"])
        fmt = lambda v, f: "" if v is None else format(v, f)                       # noqa: E731
        if kind != "other" or self.args.show_other:
            self.add_row(pk, d, fmt)
        self.open_csv()
        if self.csv and self.source != "demo":
            self.csv.writerow([datetime.datetime.fromtimestamp(pk["wall"]).isoformat(timespec="milliseconds"),
                               f"{pk['stream_s']:.6f}", kind, fmt(pk["dt_ms"], ".3f"),
                               fmt(pk["dur_ms"], ".3f"), f"{pk['level_db']:.1f}", f"{pk['snr_db']:.1f}",
                               fmt(pk["cfo_khz"], ".2f"), fmt(pk["length"], "d"), pk["header_ok"],
                               pk["crc_ok"], pk["sync"], pk["data"].hex(), text,
                               "" if kind != "ok" or d.seq is None else d.seq])

    def add_row(self, pk, d, fmt):
        kind = pk["kind"]
        label, color = self.kind[kind]
        style = "text"
        if kind == "other":
            msg = "Not LoRa (Wi-Fi, Bluetooth…)"
        elif not pk["data"]:
            msg = "(empty)" if kind == "ok" else "Couldn't read this packet"
        else:
            msg, style = d.shown, d.style
        quality = "" if kind == "other" else lh.quality_rating(pk["snr_db"]).word
        crc = "" if pk["crc_ok"] is None else ("ok" if pk["crc_ok"] and pk["header_ok"] else "BAD")
        row = [datetime.datetime.fromtimestamp(pk["wall"]).strftime("%H:%M:%S.%f")[:-3], label, quality, msg,
               fmt(pk["dt_ms"], ".1f"), fmt(pk["length"], "d"), crc, f"{pk['level_db']:.1f}",
               f"{pk['snr_db']:.1f}", fmt(pk["cfo_khz"], "+.1f"), fmt(pk["dur_ms"], ".2f"), pk["data"].hex(" ")]
        self.table.insertRow(0)
        for i, v in enumerate(row):
            it = QtWidgets.QTableWidgetItem(v)
            if i == 1:
                it.setForeground(QtGui.QColor(color))
            elif i in (2, 3) and kind != "ok":
                it.setForeground(QtGui.QColor(self.c["muted"]))
            if i == 11 or (i == 3 and style == "hex"):
                it.setFont(self.mono)
            if i == 3 and kind == "bad":
                it.setToolTip("Damaged packet: this content may be wrong.")
            elif i == 3:
                it.setToolTip({"hex": "Binary data (not text), shown as bytes in hex.",
                               "counter": "The node's packet number. Gaps in the numbers are counted as lost "
                                          "packets."}.get(style, ""))
            self.table.setItem(0, i, it)
        if self.table.rowCount() > MAX_ROWS:
            self.table.removeRow(MAX_ROWS)
        self.resize_due = True

    def data_lost(self):
        if time.time() - self.drop_seen < lh.WINDOW_S:
            return True
        return self.source == "hackrf" and self.rate is not None and time.time() - self.run_t0 > 3 and \
            self.rate < am.FS * 0.995

    def refresh(self):
        while self.recent and self.recent[0][0] < self.now - lh.WINDOW_S:
            self.recent.popleft()
        if self.fb:
            self.fb_tick()
        starting = {"hackrf": "Waking up the HackRF…", "demo": "Preparing the demo…",
                    "file": "Opening the recording…"}[self.source]
        self.status = lh.link_status(running=self.worker is not None, have_data=self.have_data, error=self.error,
                                     finished=self.finished, recent=list(self.recent), now=self.now,
                                     ever_lora=self.ever_lora, data_lost=self.data_lost(),
                                     can_tune=self.source == "hackrf", lna=self.gains()[0], vga=self.gains()[1],
                                     starting=self.worker_msg or starting,
                                     loss_pct=self.seqs.loss_pct if self.seqs.active else None)
        now, link = time.time(), None
        if self.waiting:
            self.status = lh.waiting_status(self.error)
        elif self.fb:
            fb = self.fb
            self.status = lh.Status("idle", "Finding the best sensitivity…",
                                    f"Trying {fb['names'][fb['i']]} ({fb['i'] + 1} of {len(fb['names'])}). "
                                    f"This takes about 15 s.", "cancel")
        elif self.notice and now < self.notice[1] and self.worker:
            self.status = self.notice[0]
        elif self.worker is None and not self.error and self.last_session:
            self.status = self.status._replace(detail=self.last_session.one_line(now))
            link = "View summary"
        self.status_card.set(self.status, tech=self.error, link=link)
        if self.worker and self.have_data and not self.fb:
            event = self.alert.update(now, any(r[1] == "ok" for r in self.recent))
            if event and self.alerts_on:
                notify(self, *{"dropped": ("LoRa link dropped", "No packets from your nodes for 5 s."),
                               "back": ("LoRa link is back", "Packets are arriving again.")}[event])
        if self.session and now - self.last_hist >= 1.0:
            self.update_history(now)
        self.status_card.setVisible(self.stack.currentIndex() == 1 or bool(self.error))
        self.tile_refresh()
        if self.rec_path:
            try:
                size = os.path.getsize(self.rec_path) / 1e6
            except OSError:
                size = 0.0
            el = int(time.time() - self.rec_t0)
            self.rec_pill.setText(f"● Recording {el // 60}:{el % 60:02d} · {size:,.0f} MB")
        self.rec_pill.setVisible(bool(self.rec_path))
        if self.details and self.rate is not None:
            bad = self.data_lost()
            self.health.setText(f"input {self.rate / 1e6:.3f} MS/s · dropped blocks {self.dropped} · "
                                f"backlog {self.backlog * 50} ms")
            self.health.setStyleSheet(f"color: {self.c['bad']};" if bad else "")
        if self.resize_due and time.time() - self.last_resize > 1:
            self.fit_columns()
            self.resize_due, self.last_resize = False, time.time()

    def fit_columns(self):
        """Size the visible columns to their contents, except a stretched one (resizing it would undo the stretch)."""
        hh = self.table.horizontalHeader()
        for i in range(len(COLS)):
            if not self.table.isColumnHidden(i) and \
                    hh.sectionResizeMode(i) != QtWidgets.QHeaderView.ResizeMode.Stretch:
                self.table.resizeColumnToContents(i)

    def tile_refresh(self):
        if not hasattr(self, "status"):
            return
        c, T = self.c, self.tiles
        T["ok"].set(str(self.count["ok"]), c["ok"] if self.count["ok"] else None)
        if self.seqs.active:                        # the packets carry a counter: show what was lost
            pct, level = self.seqs.loss_pct, lh.loss_level(self.seqs.loss_pct)
            T["bad"].set_title("Lost", "Packets the nodes sent that did not arrive intact, worked out from their "
                                       "packet numbers: damaged (heard, but failed the check) plus not heard at "
                                       "all. Since Start (or since Clear).")
            T["bad"].set(f"{pct:.1f} %", c[level], caption=f"{self.seqs.damaged} damaged · "
                                                           f"{self.seqs.not_heard} not heard")
        else:
            T["bad"].set_title("Damaged", "LoRa packets that were heard but failed their check (CRC), so their "
                                          "content can't be trusted.")
            T["bad"].set(str(self.count["bad"]), c["bad"] if self.count["bad"] else None)
        T["other"].set(str(self.count["other"]))
        n_ok = sum(1 for r in self.recent if r[1] == "ok")
        T["rate"].set(f"{n_ok / min(lh.WINDOW_S, max(self.now, 1.0)):.1f}" if self.have_data else "–")
        pk = self.last_lora
        if pk is None:
            for k in ("signal", "quality", "interval", "cfo"):
                T[k].set("–")
            return
        # Signal and Quality use the same last-5-s summary as the status line, so they never disagree with it.
        # With no LoRa packet in that window, show the last packet's values, greyed out.
        summary = lh.window_summary(self.recent)
        stale = self.worker is None or summary is None
        if summary:
            level, snr = summary
            cap_level, cap_snr = f"peak {level:.1f} dBFS · last 5 s", f"median {snr:.1f} dB SNR · last 5 s"
        else:
            level, snr = pk["level_db"], pk["snr_db"]
            cap_level, cap_snr = f"{level:.1f} dBFS · last packet", f"{snr:.1f} dB SNR · last packet"

        def show(tile, r, caption):
            text_color = c["muted"] if stale else None if r.level == "ok" else c[r.level]
            bar_color = c["muted"] if stale else c[r.level]
            tile.set(r.word, text_color, r.bars, bar_color, caption if self.details else None)
        show(T["signal"], lh.signal_rating(level), cap_level)
        show(T["quality"], lh.quality_rating(snr), cap_snr)
        T["interval"].set("–" if pk["dt_ms"] is None else f"{pk['dt_ms']:.1f} ms")
        T["cfo"].set("–" if pk["cfo_khz"] is None else f"{pk['cfo_khz']:+.1f} kHz")


def setup_app(app, theme=None):
    """Apply the light or dark theme (default: follow the system) and return its colours."""
    app.setApplicationName("Air Monitor")
    c = (DARK if theme == "dark" else LIGHT) if theme else DARK if is_dark(app) else LIGHT
    app.setStyle("Fusion")
    app.setPalette(palette(c))
    app.setStyleSheet(stylesheet(c))
    pg.setConfigOptions(antialias=False, background=c["card"], foreground=c["muted"])
    return c


def run(args):
    if getattr(args, "radio", None) is not None:
        am.configure(args.radio)                  # the receiver module this window (and its Worker) uses
    app = QtWidgets.QApplication(sys.argv)
    c = setup_app(app, args.theme)
    if args.screenshot and not args.settings:     # screenshot runs must not change the user's saved choices
        args.settings = os.path.join(tempfile.gettempdir(), "air-monitor-test.ini")
    w = Main(args, c)
    w.show()
    if args.file or args.demo or args.autostart:
        QtCore.QTimer.singleShot(200, w.start)
    if args.screenshot:                               # for testing: save the window after N seconds, quit
        def snap():
            w.grab().save(args.screenshot)
            w.stop()
            app.quit()
        QtCore.QTimer.singleShot(int(args.screenshot_after * 1000), snap)
    sys.exit(app.exec())
