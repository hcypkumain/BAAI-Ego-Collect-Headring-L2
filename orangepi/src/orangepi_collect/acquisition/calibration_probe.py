"""Bounded two-camera image statistics; no recording, serial access, or images saved."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'common'))
import project_paths
import json
import statistics
import threading
import time
from collections import deque
from live_preview import publisher
from wrist_calibration import current_stamp, boot_id, CalibrationError, SLOTS


def measure(config, seconds=1.8):
    import cv2
    import numpy as np
    from orangepi_collect.recorders.camera.uvc_source import UvcSource
    cv2.setNumThreads(1)
    initial_boot = boot_id(); initial_stamp = current_stamp(config)
    cameras = config.get('wrist_cameras', {})
    paths = [cameras[k]['device'] for k in SLOTS if k in cameras]
    if len(set(paths)) != 2 or len(initial_stamp) != 2:
        raise CalibrationError('two_cameras_required')
    records = {p: deque(maxlen=60) for p in paths}
    guard = threading.Lock(); sources = []; starts = [float('inf')]

    def receive(path, slot):
        def frame(payload, pts, index, mono, wall, captured):
            publisher().camera(slot, payload)
            if time.monotonic() < starts[0] or captured < int(starts[0]*1e9): return
            image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
            if image is None: raise CalibrationError('invalid_jpeg')
            image = cv2.resize(image, (160, 120), interpolation=cv2.INTER_AREA)
            row = (float(image.mean()), float((image < 25).mean()))
            with guard: records[path].append(row)
        return frame
    try:
        for slot in SLOTS:
            item = cameras[slot]; sources.append(UvcSource(item, receive(item['device'], slot)))
        starts[0] = time.monotonic()+.6
        end = starts[0]+seconds
        while time.monotonic() < end:
            for source in sources: source.health()
            time.sleep(.03)
        for source in sources: source.health()
        with guard:
            result = {p: dict(frames=len(rows), mean=statistics.median([r[0] for r in rows]) if rows else 0,
                              dark=statistics.median([r[1] for r in rows]) if rows else 0)
                      for p, rows in records.items()}
        if boot_id() != initial_boot or current_stamp(config) != initial_stamp:
            raise CalibrationError('devices_changed')
        if any(x['frames'] < 12 for x in result.values()):
            raise CalibrationError('insufficient_frames')
        return dict(ok=True, statistics=result, boot_id=initial_boot, stamp=initial_stamp)
    finally:
        for source in sources: source.close()


if __name__ == '__main__':
    from scan_registry import require_member
    require_member()
    # Remain a group anchor during bounded cleanup escalation.
    import signal
    signal.signal(signal.SIGTERM,lambda *_:None)
    try:
        print(json.dumps(measure(json.load(sys.stdin)), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps(dict(ok=False, error=str(exc) if isinstance(exc, CalibrationError) else type(exc).__name__)))
        sys.exit(1)
