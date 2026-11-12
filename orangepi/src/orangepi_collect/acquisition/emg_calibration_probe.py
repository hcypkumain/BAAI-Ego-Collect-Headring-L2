"""Read both EMG receivers concurrently for bounded motion statistics, no recording."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'common'))
import copy
import fcntl
import json
import math
import os
import select
import statistics
import struct
import termios
import time
from emg_calibration import current_stamp, boot_id, CalibrationError, SLOTS


class Accumulator:
    def __init__(self):
        self.buf = bytearray(); self.emg = 0; self.imu = 0; self.bins = {}

    def feed(self, data, elapsed):
        self.buf.extend(data)
        while len(self.buf) >= 3:
            i = self.buf.find(b'\xd2\xd2\xd2')
            if i < 0:
                del self.buf[:-2]; break
            if i: del self.buf[:i]
            if len(self.buf) < 29: break
            if self.buf[3] not in (0xaa, 0xbb):
                del self.buf[0]; continue
            frame = bytes(self.buf[:29]); del self.buf[:29]
            if elapsed < 0: continue
            bin_number = int(elapsed/.25)
            window = self.bins.setdefault(bin_number, [0, 0, 0.0])
            if frame[3] == 0xaa:
                self.emg += 1; window[0] += 1
            else:
                self.imu += 1; window[1] += 1
                gyro = struct.unpack('>hhh', frame[5:11])
                window[2] += sum((value*.0012)**2 for value in gyro)

    def result(self):
        windows = [row for row in self.bins.values() if row[0] >= 10 and row[1] >= 2]
        return dict(emg_frames=self.emg, imu_frames=self.imu, windows=len(windows),
                    motion=statistics.median([math.sqrt(row[2]/row[1]) for row in windows]) if windows else 0)


def measure(config, seconds=3.0):
    initial_boot = boot_id(); initial_stamp = current_stamp(config)
    items = config.get('emg', {})
    paths = [items[k]['port'] for k in SLOTS if k in items]
    if len(set(paths)) != 2 or len(initial_stamp) != 2:
        raise CalibrationError('two_receivers_required')
    handles = {}; results = {}; originals = {}; exclusive = set()
    try:
        for path in paths:
            fd = os.open(path, os.O_RDWR|os.O_NOCTTY|os.O_NONBLOCK)
            handles[fd] = path
            fcntl.ioctl(fd, termios.TIOCEXCL); exclusive.add(fd)
            originals[fd] = termios.tcgetattr(fd); attrs = copy.deepcopy(originals[fd])
            attrs[0] = attrs[1] = attrs[3] = 0
            attrs[2] = termios.CS8|termios.CREAD|termios.CLOCAL
            attrs[4] = attrs[5] = termios.B921600
            attrs[6][termios.VMIN] = attrs[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            termios.tcflush(fd, termios.TCIFLUSH)
            results[path] = Accumulator()
        start = time.monotonic()+1.0; end = start+seconds
        while time.monotonic() < end:
            readable = select.select(list(handles), [], [], min(.05, max(0, end-time.monotonic())))[0]
            for fd in readable:
                data = os.read(fd, 8192)
                if not data: raise CalibrationError('devices_changed')
                elapsed = time.monotonic()-start
                if elapsed < seconds: results[handles[fd]].feed(data, elapsed)
        if boot_id() != initial_boot or current_stamp(config) != initial_stamp:
            raise CalibrationError('devices_changed')
        stats = {path: acc.result() for path, acc in results.items()}
        if any(row['emg_frames'] < 200 or row['imu_frames'] < 30 or row['windows'] < 8 for row in stats.values()):
            raise CalibrationError('insufficient_data')
        return dict(ok=True, statistics=stats, boot_id=initial_boot, stamp=initial_stamp)
    finally:
        for fd in handles:
            try:
                if fd in originals: termios.tcsetattr(fd, termios.TCSANOW, originals[fd])
            except OSError: pass
            try:
                if fd in exclusive: fcntl.ioctl(fd, termios.TIOCNXCL)
            except OSError: pass
            os.close(fd)


if __name__ == '__main__':
    from acquisition_time_lock import acquire
    acquire()
    from scan_registry import require_member
    require_member()
    try:
        print(json.dumps(measure(json.load(sys.stdin)), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps(dict(ok=False, error=str(exc) if isinstance(exc, CalibrationError) else type(exc).__name__)))
        sys.exit(1)
