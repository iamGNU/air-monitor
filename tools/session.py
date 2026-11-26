"""One listening session, from Start to Stop: totals, a 10-minute history in 5 s bins, the sensitivities used, and
a plain-text summary. Gain changes and reconnects stay in the same session. No Qt here (tests/test_session.py)."""
import collections
import datetime
import statistics

BIN_S = 5.0                 # history resolution
HISTORY_S = 600.0           # history length (10 min)
MAX_SAMPLES = 50_000        # levels and SNRs kept for the medians (~80 min at 10 packets/s)


class Bin:
    __slots__ = ("t0", "ok", "bad", "not_heard")

    def __init__(self, t0):
        self.t0, self.ok, self.bad, self.not_heard = t0, 0, 0, 0


class Session:
    def __init__(self, source, start):
        self.source = source                    # e.g. "live from the HackRF"
        self.start, self.end = start, None      # wall time (s)
        self.ok = self.bad = self.not_heard = self.other = 0
        self.counters = False                   # True once a packet carried a packet counter
        self.levels = collections.deque(maxlen=MAX_SAMPLES)
        self.snrs = collections.deque(maxlen=MAX_SAMPLES)
        self.peak = None
        self.bins = collections.deque()
        self.by_gain = {}                       # sensitivity label -> [ok, bad, not_heard]
        self.longest_gap = 0.0
        self.last_lora = None                   # wall time of the last LoRa packet in this run
        self.log_path = None

    def new_run(self):
        """The receiver restarted: the time it was off is not a gap in the link."""
        self.last_lora = None

    def add(self, wall, kind, level_db=None, snr_db=None, gain="", not_heard=0, counter=False):
        """One packet. kind: "ok", "bad" or "other"; not_heard: packets the counter says were missed before it."""
        if kind == "other":
            self.other += 1
            return
        b = self._bin(wall)
        g = self.by_gain.setdefault(gain, [0, 0, 0])
        if kind == "ok":
            self.ok += 1
            b.ok += 1
            g[0] += 1
        else:
            self.bad += 1
            b.bad += 1
            g[1] += 1
        self.not_heard += not_heard
        b.not_heard += not_heard
        g[2] += not_heard
        self.counters = self.counters or counter
        self.levels.append(level_db)
        self.snrs.append(snr_db)
        self.peak = level_db if self.peak is None else max(self.peak, level_db)
        if self.last_lora is not None:
            self.longest_gap = max(self.longest_gap, wall - self.last_lora)
        self.last_lora = wall

    def _bin(self, wall):
        t0 = wall - wall % BIN_S
        if not self.bins or self.bins[-1].t0 < t0:
            self.bins.append(Bin(t0))
        while self.bins and self.bins[0].t0 < t0 - HISTORY_S:
            self.bins.popleft()
        return self.bins[-1]

    # ----- results
    @property
    def active(self):
        return self.ok + self.bad + self.other > 0

    @property
    def lost(self):
        return self.bad + self.not_heard

    @staticmethod
    def loss_pct_of(ok, bad, not_heard):
        lost = bad + not_heard
        return 100.0 * lost / (ok + lost) if ok + lost else 0.0

    @property
    def loss_pct(self):
        return self.loss_pct_of(self.ok, self.bad, self.not_heard)

    def history(self, now):
        """(minutes ago, packets/s, lost) per 5 s bin over the last 10 min, oldest first. Empty bins included,
        so a dead link shows as zero; the bin still filling is left out."""
        out = []
        have = {b.t0: b for b in self.bins}
        t_now = now - now % BIN_S
        for k in range(int(HISTORY_S / BIN_S), 0, -1):
            t0 = t_now - k * BIN_S
            if t0 < self.start:                         # before Start, or only partly listened to
                continue
            b = have.get(t0)
            out.append(((t0 + BIN_S / 2 - now) / 60, (b.ok if b else 0) / BIN_S,
                        (b.bad + b.not_heard) if b else 0))
        return out

    def best_gain(self):
        """The sensitivity with the lowest loss (at least 20 packets), or None if only one was used."""
        tried = {g: v for g, v in self.by_gain.items() if v[0] + v[1] >= 20}
        if len(tried) < 2:
            return None
        return min(tried, key=lambda g: (self.loss_pct_of(*tried[g]), -tried[g][0]))

    def one_line(self, now):
        end = self.end or now
        parts = [_duration(end - self.start), f"{self.ok:,} packets".replace(",", " ")]
        parts.append(f"{self.loss_pct:.1f} % lost" if self.counters else f"{self.bad} damaged")
        best = self.best_gain()
        if best:
            parts.append(f"best at {best.split(' (')[0]}")
        return "Session: " + " · ".join(parts)

    def summary(self, now):
        end = self.end or now
        dur = max(end - self.start, 1e-9)
        stamp = lambda t: datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")  # noqa: E731
        lines = [("Source", self.source), ("Started", stamp(self.start)),
                 ("Stopped", f"{stamp(end)}  ({_duration(dur)})"),
                 ("Packets read", f"{self.ok}  ({self.ok / dur:.1f} per second)")]
        if self.counters:
            lines.append(("Lost", f"{self.lost} ({self.loss_pct:.1f} %): {self.bad} damaged, "
                                  f"{self.not_heard} not heard"))
        else:
            lines.append(("Damaged", str(self.bad)))
        lines.append(("Other signals", f"{self.other} bursts (Wi-Fi, Bluetooth…)"))
        if self.ok + self.bad:
            lines += [("Longest gap", f"{self.longest_gap:.1f} s"),
                      ("Signal", f"peak {self.peak:.1f} dBFS, median {statistics.median(self.levels):.1f} dBFS"),
                      ("Quality", f"median {statistics.median(self.snrs):.1f} dB SNR")]
        for i, (g, (ok, bad, nh)) in enumerate(x for x in self.by_gain.items() if x[0]):   # live HackRF only
            lines.append(("Sensitivity" if i == 0 else "", f"{g}: {ok} read, "
                          f"{self.loss_pct_of(ok, bad, nh):.1f} % {'lost' if self.counters else 'damaged'}"))
        best = self.best_gain()
        if best:
            lines.append(("Best", best))
        if self.log_path:
            lines.append(("Packet log", self.log_path))
        return lines

    def summary_text(self, now):
        return "Air Monitor session summary\n\n" + "\n".join(
            f"{k + ':' if k else '':15s}{v}" for k, v in self.summary(now)) + "\n"


def _duration(s):
    s = int(round(s))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60} s" if s < 600 else f"{s // 60} min"
    return f"{s // 3600} h {s % 3600 // 60} min"
