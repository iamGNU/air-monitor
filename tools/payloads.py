"""What the nodes' packets say, for the Messages list, and how many packets were lost.

The v0.1 nodes start every packet with a type byte, 0xA4 or 0xA5, then a 16-bit packet counter, low byte first,
then the data (seen on the bench): 3-byte beacons every 97 ms (a5 d7 08, a5 d8 08, ...) and longer data packets
(a4 c7 02 fd 1c ..., 140-199 bytes, 26-11-2026), all numbered from the same counter. Gaps in that counter tell
exactly how many packets did not arrive intact. Any other payload is shown as text when it reads as text, else as
hex.
"""
import collections

COUNTER_TYPES = (0xA4, 0xA5)  # first byte of a v0.1 packet; both types share one counter
RESTART_JUMP = 1000         # a counter that goes back, or jumps further than this, means the node restarted

Described = collections.namedtuple("Described", "shown seq stream style")
# style: "counter" ("Packet #2263", "Packet #711 · 137 bytes"), "text", or "hex" (binary, shown as bytes)
# stream: which counter the seq belongs to (one per node; the v0.1 packets don't say which node sent them, so
# all of them count as one stream)


def text_or_hex(data):
    """(shown, is_text): text when it reads as text (printable ASCII, ignoring trailing spaces and NUL padding),
    else the bytes in hex."""
    body = bytes(data).rstrip(b"\0 ")
    if body and all(32 <= b < 127 for b in body):
        return body.decode("ascii"), True
    return bytes(data).hex(" "), False


def describe(data):
    data = bytes(data)
    if len(data) >= 3 and data[0] in COUNTER_TYPES:
        seq = data[1] | data[2] << 8
        extra = f" · {len(data) - 3} bytes" if len(data) > 3 else ""
        return Described(f"Packet #{seq}{extra}", seq, "v0.1", "counter")
    shown, is_text = text_or_hex(data)
    return Described(shown, None, None, "text" if is_text else "hex")


class SequenceTracker:
    """Counts lost packets from the counters of the good ones. A gap between two good packets is filled first by
    the damaged packets heard in between ('damaged'); the rest were not heard at all ('not heard')."""

    def __init__(self):
        self.received = self.damaged = self.not_heard = self.restarts = 0
        self.new_run()

    def new_run(self):
        """The receiver restarted (e.g. a sensitivity change): forget the last counters, so the packets missed
        while it was restarting don't count as lost."""
        self.last = {}
        self.damaged_since = 0

    def bad(self):
        """A damaged LoRa packet was heard (its counter can't be trusted)."""
        self.damaged_since += 1

    def good(self, stream, seq):
        """A good packet with counter `seq`. Returns (damaged, not_heard) for the gap before it."""
        prev = self.last.get(stream)
        self.last[stream] = seq
        damaged_since, self.damaged_since = self.damaged_since, 0
        if prev is not None and seq == prev:
            return 0, 0                                 # the same packet twice
        self.received += 1
        if prev is None:
            return 0, 0
        gap = (seq - prev) % 65536 - 1                  # packets missing between the two (handles the wrap)
        if gap > RESTART_JUMP:
            self.restarts += 1
            return 0, 0
        damaged = min(damaged_since, gap)
        self.damaged += damaged
        self.not_heard += gap - damaged
        return damaged, gap - damaged

    @property
    def active(self):
        return self.received > 0

    @property
    def lost(self):
        return self.damaged + self.not_heard

    @property
    def loss_pct(self):
        expected = self.received + self.lost
        return 100.0 * self.lost / expected if expected else 0.0
