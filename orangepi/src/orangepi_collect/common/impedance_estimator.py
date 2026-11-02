"""Python 3.8-compatible adapter of the supplied OmniBCI SDK impedance core.

No device I/O. Values are meaningful only for confirmed matching excitation.
The established numerical reconstruction agrees with the supplied SDK source;
this module retains its API and explicit validity reasons. It performs no I/O.
"""
from dataclasses import dataclass
import math
import struct
from typing import Optional, Sequence

FREQUENCY_HZ = 31.25
AMPLITUDE_DIVISOR = 7.639437268410976  # observed binary f64; equals 6 * 4 / pi
ADC_MIN = -8388608
ADC_MAX = 8388607
WINDOW_SECONDS = 4
DATA_TIMEOUT_SECONDS = 2.0
NORMAL_RECOVERY_MAX_SAMPLES = 4096
ALGORITHM = 'omnibci-sdk-0.2.0.dev0-sin-cos-dc-31.25hz-v1'
SDK_REFERENCE = {
    'package_version': '0.2.0.dev0',
    'python_source_revision': '0b8c342',
    'preview_revision': 'f9007fd',
    'sdk_revision': '843013ba62f1f05098c8639a657585720ce66a72',
    'archive_sha256': '9ec680db73f1c9177824a538921e56685287154556f8dbac3fe415e2a46b8eb4',
    'estimator_sha256': 'afac9bf45850a738ff853cf5645708d90eb9da8b72fc645b7ad1abedee3b97e8',
    'api_sha256': 'bff292e2002809b1aa42bef8d436a1b6766ab2aa7d356c472193e3756868808b',
    'authority': 'operator-supplied development preview; no absolute hardware calibration',
}


def series_resistance_kohm(frontend):
    """Use only the confirmed readback reference, as SDK api.py:630-635."""
    if getattr(frontend, 'verified', None) is not True:
        raise ValueError('confirmed frontend reference is required for impedance')
    reference = getattr(frontend, 'reference', None)
    if reference not in ('srb1', 'srb2'):
        raise ValueError('unsupported confirmed impedance reference')
    return 4.40 if reference == 'srb2' else 9.98


@dataclass(frozen=True)
class Estimate:
    value_kohm: Optional[float]
    reason: str
    accepted_samples: int = 0
    amplitude_uv: Optional[float] = None
    dc_uv: Optional[float] = None


def _f32(value: float) -> float:
    """Match the GUI input vector's float32 storage."""
    try:
        return struct.unpack('<f', struct.pack('<f', float(value)))[0]
    except OverflowError:
        return math.copysign(math.inf, value)


def _solve3(matrix, rhs):
    """Partial-pivot elimination, matching the observed 1e-12 pivot threshold."""
    a = [list(row) for row in matrix]
    b = list(rhs)
    for col in range(3):
        pivot = max(range(col, 3), key=lambda r: abs(a[r][col]))
        if not (abs(a[pivot][col]) >= 1e-12):
            return None
        a[col], a[pivot] = a[pivot], a[col]
        b[col], b[pivot] = b[pivot], b[col]
        for row in range(col + 1, 3):
            factor = a[row][col] / a[col][col]
            for j in range(col, 3):
                a[row][j] -= factor * a[col][j]
            b[row] -= factor * b[col]
    x = [0.0] * 3
    for row in range(2, -1, -1):
        x[row] = (b[row] - sum(a[row][j] * x[j] for j in range(row + 1, 3))) / a[row][row]
    return x if all(math.isfinite(v) for v in x) else None


def estimate_channel_reference(
    eeg_uv: Sequence[float],
    sequence: Sequence[int],
    *,
    sample_rate_hz: int,
    series_kohm: float,
    valid: Sequence[bool],
    raw_counts: Sequence[int],
    enabled: bool = True,
) -> Estimate:
    """Estimate ONE already-selected measurement window, in chronological order.

    Pass <=4*fs time positions, including invalid placeholders for missing frames.
    sequence uses the original u32 sample sequence (wrapping is supported).
    Do not renumber after dropping samples. raw_counts identifies ADC rail values.
    No filtering, RMS conversion, taper, or common-average reference is applied.
    The caller owns excitation confirmation, epoch/window selection, and freshness.
    """
    n = len(eeg_uv)
    if any(len(v) != n for v in (sequence, valid, raw_counts)):
        raise ValueError('all arrays must have equal length')
    if type(sample_rate_hz) is not int or sample_rate_hz <= 0:
        raise ValueError('sample_rate_hz must be a positive integer')
    if n > 4 * sample_rate_hz:
        raise ValueError('select at most four seconds of time positions first')
    if any(not 0 <= int(s) <= 0xFFFFFFFF for s in sequence):
        raise ValueError('sequence must contain u32 values')
    if not math.isfinite(series_kohm) or series_kohm < 0:
        return Estimate(None, 'invalid_series_resistance')
    if not enabled:
        return Estimate(None, 'channel_disabled')
    if any(bool(ok) and (int(v) <= ADC_MIN or int(v) >= ADC_MAX)
           for ok, v in zip(valid, raw_counts)):
        return Estimate(None, 'adc_saturated')
    if n == 0:
        return Estimate(None, 'insufficient_valid_samples')
    start = int(sequence[0])
    ss = sc = s1 = cc = c1 = ys = yc = y1 = 0.0
    accepted = 0
    minimum, maximum = math.inf, -math.inf
    for y, index, ok in zip(eeg_uv, sequence, valid):
        y = _f32(y)
        if not bool(ok) or not math.isfinite(y):
            continue
        theta = 196.34954084936206 * ((int(index) - start) & 0xFFFFFFFF) / sample_rate_hz
        s, c = math.sin(theta), math.cos(theta)
        ss += s * s
        sc += s * c
        s1 += s
        cc += c * c
        c1 += c
        ys += y * s
        yc += y * c
        y1 += y
        minimum, maximum = min(minimum, y), max(maximum, y)
        accepted += 1
    if accepted < sample_rate_hz:
        return Estimate(None, 'insufficient_valid_samples', accepted)
    if minimum == maximum:
        return Estimate(None, 'flat_signal', accepted)
    coefficients = _solve3([[ss, sc, s1], [sc, cc, c1], [s1, c1, float(accepted)]],
                           [ys, yc, y1])
    if coefficients is None:
        return Estimate(None, 'singular_fit', accepted)
    amplitude = math.hypot(coefficients[0], coefficients[1])
    total = amplitude / AMPLITUDE_DIVISOR
    corrected = total - series_kohm
    if not math.isfinite(total) or not math.isfinite(corrected):
        return Estimate(None, 'nonfinite_result', accepted)
    return Estimate(max(0.0, corrected), 'ok', accepted, amplitude, coefficients[2])


def select_recent_positions(eeg_uv, sequence, valid, raw_counts, *, sample_rate_hz):
    """Select the last 4*fs time positions; insert NaN/invalid gap placeholders.

    Adapter design for Python's unpadded SampleBatch, not original GUI code.
    Reject ambiguous backward/repeated indices. Use within one generation/epoch.
    Only the final four seconds are materialized, even after a large forward gap.
    """
    n = len(sequence)
    if any(len(v) != n for v in (eeg_uv, valid, raw_counts)):
        raise ValueError('all arrays must have equal length')
    if type(sample_rate_hz) is not int or sample_rate_hz <= 0:
        raise ValueError('sample_rate_hz must be a positive integer')
    if not n:
        return [], [], [], []
    seq = [int(s) for s in sequence]
    if any(not 0 <= s <= 0xFFFFFFFF for s in seq):
        raise ValueError('sequence must contain u32 values')
    positions = [0]
    for before, after in zip(seq, seq[1:]):
        delta = (after - before) & 0xFFFFFFFF
        if delta == 0 or delta >= 0x80000000:
            raise ValueError('duplicate/backward sequence; split epochs first')
        positions.append(positions[-1] + delta)
    first = max(0, positions[-1] - 4 * sample_rate_hz + 1)
    out = {p: (y, s, ok, count) for p, y, s, ok, count
           in zip(positions, eeg_uv, seq, valid, raw_counts) if p >= first}
    result = [[], [], [], []]
    for p in range(first, positions[-1] + 1):
        row = out.get(p, (math.nan, (seq[0] + p) & 0xFFFFFFFF, False, 0))
        for dest, value in zip(result, row):
            dest.append(value)
    return tuple(result)
