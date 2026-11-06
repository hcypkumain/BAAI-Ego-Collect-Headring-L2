"""ICM20602 board IMU read straight from its kernel character device.

EgoRecord v0.1.10 retired the seeker_imu ROS node and its /imu_data_raw topic;
the device exposes 200Hz fixed-length frames on /dev/icm20602 instead. Only one
process may hold the device open at a time.

Timestamps are the driver's sampling instants. They are already Unix seconds on
this platform, which puts them in the same clock domain as camd's frame
timestamps; the offset fallback below covers a driver that reports a boot-
relative clock instead, so alignment never silently drifts.
"""
import math
import json
import sys
import os
import struct
import threading
import time

DEVICE = '/dev/icm20602'
FRAME = struct.Struct('<qq7i4x')
FRAME_BYTES = FRAME.size          # 48 bytes
ACCEL_SCALE = 9.8 / 2048.0        # ADC -> m/s^2
GYRO_SCALE = (math.pi / 180.0) / 16.4   # ADC -> rad/s
EXPECTED_RATE_HZ = 200
# Beyond a day apart the driver clock cannot be the real-time clock.
CLOCK_SANITY_SECONDS = 86400


class ImuError(RuntimeError):
    pass


class Icm20602Source:
    """Reader thread delivering callback(stamp_ns, sequence, gyro, accel).

    gyro is rad/s and accel is m/s^2, both as (x, y, z) tuples, matching the
    units the previous ROS sensor_msgs/Imu payload carried.
    """

    def __init__(self, callback, device=DEVICE):
        self.callback = callback
        self.device = device
        self.failure = None
        self.frames = 0
        self.dropped_partial = 0
        self.time_offset = None
        self.timestamp_anomaly_count = 0
        self._previous_timestamp_evidence = None
        self._read_batch_id = 0
        self.last_frame = time.monotonic()
        self._stop = threading.Event()
        try:
            self.fd = os.open(device, os.O_RDONLY)
        except FileNotFoundError as error:
            raise ImuError('%s is missing; the IMU driver is not loaded' % device) from error
        except PermissionError as error:
            raise ImuError('%s is not readable by this user; check the udev rule '
                           '(KERNEL=="icm20602", MODE="0660", GROUP="orangepi")'
                           % device) from error
        except OSError as error:
            if getattr(error, 'errno', None) == 16:  # EBUSY
                raise ImuError('%s is already open by another program' % device) from error
            raise ImuError('cannot open %s: %s' % (device, error)) from error
        self.description = ('%s direct read, %d Hz, %d-byte frames'
                            % (device, EXPECTED_RATE_HZ, FRAME_BYTES))
        self._thread = threading.Thread(target=self._run, name='icm20602', daemon=True)
        self._thread.start()

    def _stamp_ns(self, seconds, nanoseconds):
        raw = seconds + nanoseconds * 1e-9
        if self.time_offset is None:
            # Anchor once: zero when the driver already reports Unix time.
            self.time_offset = (0.0 if abs(raw - time.time()) <= CLOCK_SANITY_SECONDS
                                else time.time() - raw)
        return int(round((raw + self.time_offset) * 1e9))

    def _record_timestamp_evidence(self, seconds, nanoseconds, stamp, frame):
        # Evidence only: retain the existing timestamp and failure policy.
        sample = (int(seconds), int(nanoseconds), int(stamp), bytes(frame),
                  self._read_batch_id, self.frames + 1)
        previous = self._previous_timestamp_evidence
        self._previous_timestamp_evidence = sample
        if previous is None or sample[2] > previous[2]:
            return
        self.timestamp_anomaly_count += 1
        if self.timestamp_anomaly_count > 8:
            return
        raw_ns = sample[0] * 1000000000 + sample[1]
        previous_raw_ns = previous[0] * 1000000000 + previous[1]
        delta = raw_ns - previous_raw_ns
        kind = ('raw_driver_timestamp_duplicate' if delta == 0 else
                'raw_driver_timestamp_backwards' if delta < 0 else
                'timestamp_conversion_collision')
        event = dict(event='imu_timestamp_evidence', classification=kind,
                     device=self.device, anomaly_count=self.timestamp_anomaly_count,
                     raw_delta_ns=delta, converted_delta_ns=sample[2]-previous[2],
                     frame_identical=sample[3] == previous[3],
                     previous=dict(seconds=previous[0], nanoseconds=previous[1],
                                   converted_ns=previous[2], frame_hex=previous[3].hex(),
                                   read_batch=previous[4], sequence=previous[5]),
                     current=dict(seconds=sample[0], nanoseconds=sample[1],
                                  converted_ns=sample[2], frame_hex=sample[3].hex(),
                                  read_batch=sample[4], sequence=sample[5]),
                     time_offset_seconds=self.time_offset,
                     arrival_unix_ns=time.time_ns(), arrival_monotonic_ns=time.monotonic_ns())
        try:
            print('IMU_TIMESTAMP_EVIDENCE '+json.dumps(event,sort_keys=True),
                  file=sys.stderr,flush=True)
        except OSError:
            pass

    def _run(self):
        buffer = bytearray()
        try:
            while not self._stop.is_set():
                chunk = os.read(self.fd, FRAME_BYTES * 16)
                if not chunk:
                    raise ImuError('%s returned end of file' % self.device)
                self._read_batch_id += 1
                buffer.extend(chunk)
                # Short reads are normal; accumulate until a frame is complete.
                while len(buffer) >= FRAME_BYTES:
                    frame = bytes(buffer[:FRAME_BYTES])
                    del buffer[:FRAME_BYTES]
                    (seconds, nanoseconds, gyro_x, gyro_y, gyro_z,
                     accel_x, accel_y, accel_z, _temperature) = FRAME.unpack(frame)
                    stamp = self._stamp_ns(seconds, nanoseconds)
                    self._record_timestamp_evidence(seconds, nanoseconds, stamp, frame)
                    self.frames += 1
                    self.last_frame = time.monotonic()
                    self.callback(
                        stamp, self.frames,
                        (gyro_x * GYRO_SCALE, gyro_y * GYRO_SCALE, gyro_z * GYRO_SCALE),
                        (accel_x * ACCEL_SCALE, accel_y * ACCEL_SCALE, accel_z * ACCEL_SCALE))
        except Exception as error:  # surfaced through health()
            if not self._stop.is_set():
                self.failure = error

    def health(self):
        if self.failure is not None:
            raise ImuError(str(self.failure))

    def close(self):
        self._stop.set()
        try:
            os.close(self.fd)
        except OSError:
            pass
        self._thread.join(5)
