"""Disable known unused channels in hardware and remove them from BIAS/SRB2."""
from dataclasses import replace


def enable_all_channels(board):
    """Enable CH1–8 with ACK, preserving gain, mode and existing channel routing.

    Newly enabled inputs join the current common reference and, when in use,
    BIAS sensing. In particular this restores CH1 after the former 7ch profile
    removed it from all three masks. Already enabled inputs keep their routing.
    """
    before = board.get_config()
    added = 255 & ~before.enabled_mask
    desired = replace(before, enabled_mask=255,
                      bias_mask=before.bias_mask | (added if before.bias_mask else 0),
                      srb2_mask=before.srb2_mask | (added if before.reference == 'srb2' else 0))
    fields = ('reference', 'mode', 'enabled_mask', 'bias_mask', 'srb2_mask', 'gains')
    if any(getattr(desired, key) != getattr(before, key) for key in fields):
        board.configure(desired, timeout=5)
    confirmed = board.get_config()
    if not confirmed.verified or any(getattr(confirmed, key) != getattr(desired, key) for key in fields):
        raise RuntimeError('Eight-channel hardware configuration was not acknowledged; refusing to start acquisition')
    return before, confirmed


def parse_channel_numbers(value):
    if not value:
        return ()
    channels = tuple(int(part.strip()) for part in value.split(','))
    if not channels or any(not 1 <= n <= 8 for n in channels) or len(set(channels)) != len(channels):
        raise ValueError('Use distinct physical channel numbers 1..8, e.g. --disable-channels 1')
    return channels


def disable_channels(board, channels):
    """Preserve gain/reference/mode and existing disables; require firmware ACK."""
    channels = tuple(channels)
    if any(type(n) is not int or not 1 <= n <= 8 for n in channels):
        raise ValueError('Channel numbers must be integers in 1..8')
    before = board.get_config()
    disabled_mask = sum(1 << (n - 1) for n in set(channels))
    enabled = before.enabled_mask & ~disabled_mask
    if not enabled:
        raise ValueError('At least one measurement channel must remain enabled')
    desired = replace(before, enabled_mask=enabled, bias_mask=before.bias_mask & enabled,
                      srb2_mask=before.srb2_mask & enabled)
    fields = ('reference', 'mode', 'enabled_mask', 'bias_mask', 'srb2_mask', 'gains')
    if any(getattr(desired, key) != getattr(before, key) for key in fields):
        board.configure(desired, timeout=5)
    confirmed = board.get_config()
    if not confirmed.verified or any(getattr(confirmed, key) != getattr(desired, key) for key in fields):
        raise RuntimeError('Hardware configuration was not acknowledged; refusing to start acquisition')
    return before, confirmed
