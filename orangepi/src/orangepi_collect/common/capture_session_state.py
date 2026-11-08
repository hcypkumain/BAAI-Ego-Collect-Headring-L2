"""Active ownership is not the last completed segment kept for replay/retry."""
IDLE_STATES=frozenset(('device_confirm','idle','failed_wait','exhausted','stopped'))

def active_session(owner):
    path=getattr(owner,'path',None)
    if path is None:return None
    # A dead-but-not-reaped worker still belongs to finalization. Do not allow
    # background reads or USB role changes until the controller finishes it.
    if getattr(owner,'worker',None) is not None:return str(path)
    if getattr(owner,'state',None) in IDLE_STATES:return None
    return str(path)
