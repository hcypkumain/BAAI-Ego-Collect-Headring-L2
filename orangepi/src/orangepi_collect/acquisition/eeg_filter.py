"""Independent causal filters; workstation coefficients are not publicly verified."""
from dataclasses import asdict, dataclass
import numpy as np


@dataclass(frozen=True)
class FilterSettings:
    highpass_hz: float | None = None
    lowpass_hz: float | None = None
    notch_hz: tuple[float, ...] = ()
    order: int = 2
    notch_q: float = 30.0
    average_reference: bool = False

    @property
    def active(self):
        return bool(self.highpass_hz or self.lowpass_hz or self.notch_hz or self.average_reference)

    def metadata(self):
        return asdict(self) | {
            "implementation": "SciPy causal SOS; high-pass, low-pass, notches, then optional common average",
            "initialization": "step steady-state at first valid sample; reset after missing/invalid sample",
            "official_equivalence": "cutoff controls matched; official order, Q, coefficients and processing order unverified",
            "raw_data_preserved": True,
        }


class StreamingFilter:
    def __init__(self, settings: FilterSettings, sample_rate=250, enabled_channels=None):
        from scipy import signal
        self.signal, self.settings, self.sample_rate = signal, settings, sample_rate
        self.enabled_channels = np.ones(8, dtype=bool) if enabled_channels is None else np.asarray(enabled_channels, dtype=bool)
        if self.enabled_channels.shape != (8,) or not self.enabled_channels.any():
            raise ValueError('Filter requires eight channel flags with at least one enabled channel')
        if not 1 <= settings.order <= 10 or not np.isfinite(settings.notch_q) or settings.notch_q <= 0:
            raise ValueError("Filter order must be 1..10; notch Q must be finite and positive")
        frequencies = [v for v in (settings.highpass_hz, settings.lowpass_hz) if v is not None] + list(settings.notch_hz)
        if any(not np.isfinite(v) or not 0 < v < sample_rate / 2 for v in frequencies):
            raise ValueError("Filter frequencies must lie strictly between zero and Nyquist")
        if settings.highpass_hz and settings.lowpass_hz and settings.highpass_hz >= settings.lowpass_hz:
            raise ValueError("High-pass cutoff must be below low-pass cutoff")
        sections = []
        if settings.highpass_hz:
            sections.append(signal.butter(settings.order, settings.highpass_hz, btype="highpass", fs=sample_rate, output="sos"))
        if settings.lowpass_hz:
            sections.append(signal.butter(settings.order, settings.lowpass_hz, btype="lowpass", fs=sample_rate, output="sos"))
        for f in settings.notch_hz:
            b, a = signal.iirnotch(f, settings.notch_q, fs=sample_rate)
            sections.append(signal.tf2sos(b, a))
        self.sos = np.vstack(sections) if sections else np.empty((0, 6))
        self.initial_zi = signal.sosfilt_zi(self.sos) if len(self.sos) else None
        self.zi = None
        self.last_index = None

    def apply(self, values, indices, valid):
        values = np.asarray(values, dtype=np.float64)
        indices = np.asarray(indices, dtype=np.int64)
        valid = np.asarray(valid, dtype=bool)
        result = np.full(values.shape, np.nan)
        if len(values) != len(indices) or len(values) != len(valid):
            raise ValueError("Filter input lengths disagree")
        if not len(values):
            return result.astype(np.float32)
        good = valid & np.isfinite(values[:, self.enabled_channels]).all(axis=1)
        # Filter entire contiguous valid runs in native SciPy, instead of
        # calling sosfilt once per sample. Preserve exactly the previous state
        # and reset semantics at invalid rows and sample-index gaps.
        breaks = np.flatnonzero((np.diff(indices) != 1) | (good[1:] != good[:-1])) + 1
        edges = np.r_[0, breaks, len(values)]
        for start, end in zip(edges[:-1], edges[1:]):
            if self.last_index is not None and indices[start] != self.last_index + 1:
                self.zi = None
            self.last_index = int(indices[end - 1])
            if not good[start]:
                self.zi = None
                continue
            run = values[start:end, self.enabled_channels]
            if len(self.sos):
                if self.zi is None:
                    self.zi = self.initial_zi[:, :, None] * run[0][None, None, :]
                run, self.zi = self.signal.sosfilt(self.sos, run, axis=0, zi=self.zi)
            if self.settings.average_reference:
                run = run - run.mean(axis=1, keepdims=True)
            result[start:end, self.enabled_channels] = run
        return result.astype(np.float32)
