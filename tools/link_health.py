"""What the monitor's numbers mean, in plain words: the link status line, the signal and quality ratings, the
sensitivity presets and advice, and friendly HackRF error messages.

No Qt here, so it can be tested on its own: python3 -m unittest discover tests
The thresholds are starting points, to be tuned on the bench.
"""
import collections
import statistics

WINDOW_S = 5.0          # the status looks at the last 5 s of packets
CLIP_DB = -12.0         # LoRa level above this (dBFS): the HackRF is close to clipping
WEAK_SNR_DB = 0.0       # median LoRa SNR below this: the signal is weak (SF7 still decodes to about -7.5 dB)
ERROR_RATIO = 0.2       # share of damaged LoRa packets that turns the status amber (at least ERROR_MIN of them)
ERROR_MIN = 2

# Sensitivity presets: (LNA dB, VGA dB). Normal is the old default. Low and High are first guesses.
SENSITIVITY = {"Low": (8, 10), "Normal": (16, 20), "High": (32, 30)}
ORDER = ["Low", "Normal", "High"]

Status = collections.namedtuple("Status", "level headline detail action")
# level:  "idle" (grey), "ok" (green), "warn" (amber), "bad" (red)
# action: None, "lower" or "raise" (the sensitivity), offered as a "Fix it" button

Rating = collections.namedtuple("Rating", "bars word level")


# --------------------------------------------------------------------------------------------- ratings
def signal_rating(level_db):
    """How loud the nodes are at the HackRF. 'Too strong' means it is overloaded."""
    if level_db is None:
        return Rating(0, "–", "idle")
    if level_db > CLIP_DB:
        return Rating(5, "Too strong", "bad")
    for limit, bars, word in ((-25, 5, "Strong"), (-40, 4, "Good"), (-55, 3, "Low"), (-70, 2, "Very low")):
        if level_db > limit:
            return Rating(bars, word, "ok")
    return Rating(1, "Very low", "ok")


def quality_rating(snr_db):
    """How clearly the packets stand out from the noise."""
    if snr_db is None:
        return Rating(0, "–", "idle")
    for limit, bars, word, level in ((10, 5, "Excellent", "ok"), (5, 4, "Good", "ok"), (0, 3, "Fair", "ok"),
                                     (-5, 2, "Weak", "warn")):
        if snr_db >= limit:
            return Rating(bars, word, level)
    return Rating(1, "Very weak", "warn")


# --------------------------------------------------------------------------------------------- sensitivity
def preset_name(lna, vga):
    """'Low', 'Normal', 'High', or None for a custom gain."""
    for name, gains in SENSITIVITY.items():
        if gains == (lna, vga):
            return name
    return None


def step_sensitivity(lna, vga, direction):
    """The next preset below (direction < 0) or above (> 0) the current total gain, or None at the end."""
    total = lna + vga
    presets = sorted(SENSITIVITY, key=lambda n: sum(SENSITIVITY[n]))
    if direction < 0:
        lower = [n for n in presets if sum(SENSITIVITY[n]) < total]
        return lower[-1] if lower else None
    higher = [n for n in presets if sum(SENSITIVITY[n]) > total]
    return higher[0] if higher else None


def gain_advice(recent):
    """'lower' if a node is overloading the HackRF, 'raise' if the packets are weak, else None.
    recent: (stream_s, kind, level_db, snr_db) tuples."""
    lora = [r for r in recent if r[1] in ("ok", "bad")]
    if not lora:
        return None
    if max(r[2] for r in lora) > CLIP_DB:
        return "lower"
    if statistics.median(r[3] for r in lora) < WEAK_SNR_DB:
        return "raise"
    return None


# --------------------------------------------------------------------------------------------- status
def _ago(seconds):
    return "just now" if seconds < 1 else f"{seconds:.0f} s ago"


def link_status(*, running, have_data=False, error=None, finished=False, recent=(), now=0.0, ever_lora=False,
                data_lost=False, can_tune=True, lna=16, vga=20, starting="Waking up the HackRF…"):
    """The one-line answer to 'is my link working?', checked in order of importance.
    recent: (stream_s, kind, level_db, snr_db) for the last WINDOW_S seconds; now: stream seconds of this run."""
    if error:
        head, detail = friendly_error(error)
        return Status("bad", head, detail, None)
    if finished:
        return Status("idle", "Recording finished",
                      "Press Start to play it again, or choose another source from the ⋯ menu.", None)
    if not running:
        return Status("idle", "Stopped", "Press Start to listen.", None)
    if not have_data:
        return Status("idle", "Starting…", starting, None)

    advice = gain_advice(recent)
    can = {"lower": can_tune and step_sensitivity(lna, vga, -1) is not None,
           "raise": can_tune and step_sensitivity(lna, vga, +1) is not None}
    if advice == "lower":
        if can["lower"]:
            return Status("bad", "Signal too strong",
                          "A node is overloading the HackRF, so packets can be lost. Lower the sensitivity, "
                          "or move the HackRF further from the node.", "lower")
        return Status("bad", "Signal too strong",
                      "A node is overloading the HackRF, so packets can be lost. Move the HackRF further "
                      "from the node (at least 1 m)." if can_tune else
                      "This recording was made with the signal too strong, so packets may be missing.", None)
    if data_lost:
        return Status("warn", "Some radio data was lost",
                      "The computer couldn't keep up with the HackRF. Close other heavy programs, or plug the "
                      "HackRF straight into the computer instead of a hub.", None)

    lora = [r for r in recent if r[1] in ("ok", "bad")]
    ok = [r for r in lora if r[1] == "ok"]
    bad = len(lora) - len(ok)
    weak = advice == "raise"
    fix_weak = "raise" if weak and can["raise"] else None
    weak_hint = " Raise the sensitivity or move the HackRF closer." if fix_weak else \
        " Move the HackRF closer to the nodes." if weak and can_tune else ""
    if bad >= ERROR_MIN and bad / len(lora) >= ERROR_RATIO:
        detail = f"{bad} of {len(lora)} packets in the last {WINDOW_S:.0f} s failed their check."
        detail += " The signal is weak." + weak_hint if weak else \
            " Another transmitter may be talking at the same time, or a node may be too far away."
        return Status("warn", "Some packets are damaged", detail, fix_weak)
    if ok:
        window = min(WINDOW_S, max(now, 1.0))
        detail = f"{len(ok) / window:.1f} packets/s · last one {_ago(now - ok[-1][0])}"
        if weak:
            return Status("warn", "Link is working, but the signal is weak", detail + "." + weak_hint, fix_weak)
        return Status("ok", "Link is working", detail, None)

    others = sum(1 for r in recent if r[1] == "other")
    head = f"No LoRa packets in the last {WINDOW_S:.0f} s" if ever_lora else "Listening, no LoRa packets yet"
    detail = ("Other signals are on the channel (Wi-Fi, Bluetooth…), but nothing from your nodes. "
              if others else "The channel is quiet. ") + "Is a node switched on and transmitting?"
    return Status("warn" if ever_lora else "idle", head, detail, None)


# --------------------------------------------------------------------------------------------- errors
def friendly_error(raw):
    """(headline, what to do) for an error from hackrf_transfer or the receiver. The raw text stays available."""
    t = (raw or "").lower().replace("_", " ")          # HACKRF_ERROR_NOT_FOUND -> "... not found"
    if "hackrf transfer" in t and ("no such file" in t or "not installed" in t):
        return ("HackRF software isn't installed",
                "Install it with  sudo apt install hackrf  and then press Start again.")
    if "not found" in t or "no hackrf boards" in t:
        return ("HackRF not found",
                "Check that the HackRF is plugged in (try another USB port or cable), then press Start.")
    if "busy" in t:
        return ("The HackRF is in use",
                "Another program (for example Gqrx or SDR++) is using it. Close that program, then press Start.")
    if "access" in t or "permission" in t:
        return ("No permission to use the HackRF",
                "Install the HackRF udev rules (they come with the hackrf package), then unplug and replug it.")
    if "usb error" in t or "libusb" in t:
        return ("The HackRF couldn't be opened",
                "Close any other radio program (for example Gqrx), unplug and replug the HackRF, then press Start.")
    if "no such file" in t:
        return ("Recording not found", "The file was moved or deleted. Open another one from the ⋯ menu.")
    return ("The HackRF stopped unexpectedly", "Check the USB cable, then press Start again.")
