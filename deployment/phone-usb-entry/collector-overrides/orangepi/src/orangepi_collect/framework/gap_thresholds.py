"""Integer boundary rules shared by live capture and offline integrity QC."""

WRIST_WARN_NS = 100_000_000
WRIST_ERROR_NS = 150_000_000
EMG_WARN_NS = 10_000_000
EMG_ERROR_NS = 150_000_000
# Headband cameras run at 30 fps (33.333 ms). Measured on EgoRecord v0.1.10,
# camd delivery occasionally skips exactly two frames, producing a 100 ms gap
# while camd still reports no drop of its own, so the loss is only visible in
# the recorded timestamps. One missing frame (>1.5 periods) warns; two or more
# (>2.5 periods, which covers the observed 100 ms defect) is an error.
HEADBAND_WARN_NS = 50_000_000
HEADBAND_ERROR_NS = 83_000_000
EVENT_LIMIT = 200


def gap_level(interval_ns, warn_ns, error_ns):
    """Strictly greater: equality never enters the higher severity band."""
    if (type(interval_ns) is not int or interval_ns < 0
            or type(warn_ns) is not int or type(error_ns) is not int
            or not 0 < warn_ns < error_ns):
        raise ValueError('Gap classification requires nonnegative integer nanoseconds.')
    if interval_ns > error_ns:
        return 'ERROR'
    if interval_ns > warn_ns:
        return 'WARN'
    return None


def wrist_gap_level(interval_ns):
    return gap_level(interval_ns, WRIST_WARN_NS, WRIST_ERROR_NS)


def headband_gap_level(interval_ns):
    return gap_level(interval_ns, HEADBAND_WARN_NS, HEADBAND_ERROR_NS)


def emg_loss_level(dropped, sent):
    """0.1% / 1% in exact integer arithmetic, with strict boundaries."""
    if (type(dropped) is not int or type(sent) is not int
            or not 0 <= dropped < sent):
        raise ValueError('Loss classification requires a positive frame budget.')
    if dropped * 100 > sent:
        return 'ERROR'
    if dropped * 1000 > sent:
        return 'WARN'
    return None
