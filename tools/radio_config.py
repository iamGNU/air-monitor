"""The nodes' radio settings, from air-monitor.toml next to the tools folder (optional; --config picks another
file). Without a file, the v0.1 firmware settings are used: 2440.0 MHz, SF7, 1625 kHz, coding rate 4/5.

    [radio]
    frequency_mhz = 2440.0
    spreading_factor = 7
    bandwidth_khz = 1625
    coding_rate = 5          # 4/5

The receiver's own numbers (sample rate, tuning offset, filter) follow from the bandwidth; for 1625 kHz they are
exactly the ones the monitor has always used.
"""
import dataclasses
import math
import os
import tomllib

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "air-monitor.toml")
BANDWIDTHS = (1_625_000, 812_500, 406_250, 203_125)       # SX1281 LoRa bandwidths (Hz)
SPREADING_FACTORS = range(7, 11)                           # the ones the receiver is tested with (SF7-SF10)
KEYS = {"frequency_mhz", "spreading_factor", "bandwidth_khz", "coding_rate"}


class ConfigError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class Radio:
    freq_hz: float = 2440.0e6
    sf: int = 7
    bw: int = 1_625_000
    cr: int = 5                             # coding rate 4/cr

    # ----- what the receiver uses, derived from the bandwidth
    @property
    def os(self):
        """Samples per chip: 4, or more for narrow bandwidths, so the HackRF runs at >= 3.25 MS/s."""
        return max(4, math.ceil(3.25e6 / self.bw))

    @property
    def fs(self):
        return float(self.os * self.bw)

    @property
    def offset(self):
        """The HackRF is tuned this far below the channel, to keep its own DC spike out of the channel."""
        return max(self.bw * 12 / 13, 0.4e6)

    @property
    def cutoff(self):
        """Channel filter cut-off (Hz)."""
        return float(round(self.bw * 0.95e6 / 1.625e6))

    @property
    def block(self):
        """Samples per read: 50 ms."""
        return int(round(self.fs * 0.05))

    def label(self):
        return f"{self.freq_hz / 1e6:g} MHz · SF{self.sf} · {self.bw / 1e3:g} kHz · CR 4/{self.cr}"


DEFAULT = Radio()


def validate(radio, where="radio settings"):
    errors = []
    if not 2400e6 <= radio.freq_hz <= 2500e6:
        errors.append(f"frequency_mhz = {radio.freq_hz / 1e6:g}: must be 2400-2500 (the SX1281 band)")
    if radio.sf not in SPREADING_FACTORS:
        errors.append(f"spreading_factor = {radio.sf}: must be {SPREADING_FACTORS.start}-"
                      f"{SPREADING_FACTORS.stop - 1}")
    if radio.bw not in BANDWIDTHS:
        errors.append(f"bandwidth_khz = {radio.bw / 1e3:g}: must be one of "
                      + ", ".join(f"{b / 1e3:g}" for b in BANDWIDTHS))
    if not 5 <= radio.cr <= 8:
        errors.append(f"coding_rate = {radio.cr}: must be 5-8 (for 4/5 to 4/8)")
    if errors:
        raise ConfigError("\n".join(f"{where}: {e}" for e in errors))
    return radio


def load(path=None):
    """The Radio from a TOML file: `path`, or air-monitor.toml if it exists, else the defaults."""
    explicit = path is not None
    path = path or DEFAULT_PATH
    if not os.path.exists(path):
        if explicit:
            raise ConfigError(f"{path}: file not found")
        return DEFAULT
    try:
        with open(path, "rb") as f:
            section = tomllib.load(f).get("radio", {})
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{path}: {e}") from None
    unknown = set(section) - KEYS
    if unknown:
        raise ConfigError(f"{path}: unknown setting(s) {', '.join(sorted(unknown))}; "
                          f"known: {', '.join(sorted(KEYS))}")
    try:
        radio = Radio(freq_hz=float(section.get("frequency_mhz", DEFAULT.freq_hz / 1e6)) * 1e6,
                      sf=int(section.get("spreading_factor", DEFAULT.sf)),
                      bw=int(round(float(section.get("bandwidth_khz", DEFAULT.bw / 1e3)) * 1e3)),
                      cr=int(section.get("coding_rate", DEFAULT.cr)))
    except (TypeError, ValueError) as e:
        raise ConfigError(f"{path}: {e}") from None
    return validate(radio, path)
