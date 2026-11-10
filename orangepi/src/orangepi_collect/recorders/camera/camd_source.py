"""camd client for the four headband fisheye cameras (EgoRecord >= v0.1.9).

The ROS bridge that used to publish /fisheye/*/image_raw/compressed is retired
in EgoRecord v0.1.10; camd replaces it and exposes an already-encoded H.264
Annex-B stream over a Unix socket. Each subscription is single-consumer and is
not mutually exclusive with camd's own recording.

Two properties matter for this recorder:

* ts_us is CLOCK_REALTIME microseconds on the frame grid, the same clock domain
  as /dev/icm20602 timestamps, so cross-sensor alignment needs no conversion.
  Source timestamp anomalies must still be checked by the recorder.
* The first pushed frame is always an IDR carrying SPS/PPS, because camd forces
  a keyframe when a subscription starts.
"""
import io
import json
import socket
import struct
import threading
import time

SOCKET_PATH = '/run/camd.sock'
HEADER = struct.Struct('<IIIIq')
HEADER_BYTES = HEADER.size
H264_MAGIC = 0x48323634
KEYFRAME_FLAG = 0x1
# camd names its cameras independently of our device keys.
CAMERA_NAMES = {'cam_ros_left': 'left', 'cam_ros_right': 'right',
                'cam_ros_bleft': 'bleft', 'cam_ros_bright': 'bright'}
# camd itself gives up on a stalled camera after ~2 s with "快照超时"; allow a
# little more so a healthy-but-busy camera is not failed by our own deadline.
SNAPSHOT_PROBE_TIMEOUT = 4.0


class CamdError(RuntimeError):
    pass


class _Connection:
    """Line-delimited JSON commands plus exact binary reads on one socket."""

    def __init__(self, path=SOCKET_PATH, timeout=10.0):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(timeout)
        try:
            self.socket.connect(path)
        except OSError as error:
            self.socket.close()
            raise CamdError('camd socket unavailable at %s: %s' % (path, error)) from error
        self.buffer = bytearray()

    def send(self, payload):
        self.socket.sendall((json.dumps(payload) + '\n').encode())

    def _fill(self):
        chunk = self.socket.recv(262144)
        if not chunk:
            raise CamdError('camd closed the connection')
        self.buffer += chunk

    def read_line(self, max_bytes=65536):
        while True:
            index = self.buffer.find(b'\n')
            if index >= 0:
                if index > max_bytes:
                    raise CamdError('camd JSON reply exceeds the size limit')
                line = bytes(self.buffer[:index])
                del self.buffer[:index + 1]
                return line
            if len(self.buffer) > max_bytes:
                raise CamdError('camd JSON reply exceeds the size limit')
            self._fill()

    def read_exact(self, count):
        while len(self.buffer) < count:
            self._fill()
        value = bytes(self.buffer[:count])
        del self.buffer[:count]
        return value

    def close(self):
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.socket.close()
        except OSError:
            pass


def status(path=SOCKET_PATH, timeout=8.0):
    """STATUS as a dict. Used for pre-flight checks before a capture."""
    connection = _Connection(path, timeout)
    try:
        connection.send({'cmd': 'STATUS'})
        value = json.loads(connection.read_line().decode())
    except (ValueError, UnicodeDecodeError) as error:
        raise CamdError('camd STATUS is not valid JSON: %s' % error) from error
    finally:
        connection.close()
    if not value.get('ok', True) or 'cams' not in value:
        raise CamdError('camd STATUS failed: %s' % json.dumps(value)[:200])
    return value


def preflight(names, path=SOCKET_PATH):
    """Raise unless every requested camera is idle and producing frames.

    camd must be IDLE and no other process may already hold the H.264
    subscription for a camera we need.

    Note on this build (EgoRecord v0.1.10): STATUS reports only
    ok/state/cams, without the h264_stream_enabled / jpeg_stream_enabled
    switches the integration guide describes, and STREAM_H264 works with no
    camd.conf opt-in. STREAM_JPEG is not implemented at all -- the binary
    exposes STATUS, START, STOP, SNAPSHOT, STREAM_H264 and STREAM_RAW.
    """
    value = status(path)
    state = value.get('state')
    if state != 'IDLE':
        raise CamdError('camd is busy (state=%s); another program may be recording' % state)
    cams = value.get('cams') or {}
    for name in names:
        camera = CAMERA_NAMES.get(name, name)
        entry = cams.get(camera)
        if entry is None:
            raise CamdError('camd does not expose camera %r' % camera)
        if entry.get('h264_stream'):
            raise CamdError('camera %r already has an H.264 subscriber' % camera)
        if not entry.get('snapshot_ready', True):
            raise CamdError('camera %r has not produced a frame yet' % camera)
    # STATUS is not sufficient on its own: a camera whose sensor has stopped
    # delivering was still reported as snapshot_ready=True with fps=30, while
    # both SNAPSHOT and STREAM_H264 timed out. Prove each camera really yields a
    # frame before a capture commits to it.
    for name in names:
        camera = CAMERA_NAMES.get(name, name)
        try:
            snapshot(name, path=path, timeout=SNAPSHOT_PROBE_TIMEOUT)
        except CamdError as error:
            raise CamdError('camera %r reports ready but delivers no frame: %s'
                            % (camera, error)) from error
    return value


NV12_MAGIC = 0x4E563132
_SNAPSHOT_MAX_PIXELS = 4096 * 4096


def snapshot(name, path=SOCKET_PATH, timeout=4.0):
    """Latest frame of one camera as (width, height, ts_us, nv12_bytes).

    Works in any camd state, including while camd records. This build has no
    STREAM_JPEG, so the live preview samples frames through SNAPSHOT instead of
    holding a second subscription.
    """
    camera = CAMERA_NAMES.get(name, name)
    connection = _Connection(path, timeout)
    try:
        connection.send({'cmd': 'SNAPSHOT', 'cam': camera})
        first = connection.read_exact(1)
        if first == b'{':
            detail = (first + connection.read_line()).decode('utf-8', 'replace')
            raise CamdError('%s: snapshot failed: %s' % (camera, detail[:200]))
        header = first + connection.read_exact(HEADER_BYTES - 1)
        magic, width, height, _reserved, ts_us = HEADER.unpack(header)
        if magic != NV12_MAGIC:
            raise CamdError('%s: unexpected snapshot magic 0x%08X' % (camera, magic))
        if (width <= 0 or height <= 0 or width % 2 or height % 2
                or width * height > _SNAPSHOT_MAX_PIXELS):
            raise CamdError('%s: implausible snapshot geometry %dx%d' % (camera, width, height))
        payload = connection.read_exact(width * height * 3 // 2)
        return width, height, ts_us, payload
    finally:
        connection.close()


def snapshot_jpeg(name, path=SOCKET_PATH, quality=70, scale=2):
    """SNAPSHOT converted to JPEG for the browser preview.

    `scale` downsamples by simple decimation before encoding: a full 1920x1200
    preview is far more data than the phone UI needs, and decimating the Y/UV
    planes costs much less than a resize filter.
    """
    width, height, ts_us, nv12 = snapshot(name, path)
    try:
        import numpy as np
        from PIL import Image
    except ImportError as error:
        raise CamdError('preview needs numpy and Pillow: %s' % error) from error
    plane = np.frombuffer(nv12, dtype=np.uint8)
    luma = plane[:width * height].reshape(height, width)
    chroma = plane[width * height:].reshape(height // 2, width // 2, 2)
    step = max(1, int(scale))
    luma = luma[::step, ::step]
    # The chroma plane is already half resolution, so decimating it by step//2
    # lands it on the decimated luma grid directly. The previous code decimated
    # by step//2 and then repeated each sample 2x2, producing an array four
    # times larger than needed and discarding three quarters of it. Every
    # conversion below holds the GIL, so with four cameras sampled in parallel
    # that waste was the preview's rate ceiling.
    chroma_step = max(1, step // 2)
    chroma = chroma[::chroma_step, ::chroma_step]
    if step < 2:
        # Only a near-full-resolution preview still needs chroma upsampling.
        chroma = np.repeat(np.repeat(chroma, 2, axis=0), 2, axis=1)
    rows = min(luma.shape[0], chroma.shape[0])
    columns = min(luma.shape[1], chroma.shape[1])
    # int32 explicitly: the BT.601 coefficients overflow int16, and only
    # numpy 1.x's value-based casting silently promoted them. Stating the width
    # keeps the same arithmetic the device already performs under numpy 1.17
    # while staying correct under NEP 50 (numpy 2).
    y = luma[:rows, :columns].astype(np.int32)
    u = chroma[:rows, :columns, 0].astype(np.int32) - 128
    v = chroma[:rows, :columns, 1].astype(np.int32) - 128
    red = np.clip(y + ((91881 * v) >> 16), 0, 255)
    green = np.clip(y - ((22554 * u + 46802 * v) >> 16), 0, 255)
    blue = np.clip(y + ((116130 * u) >> 16), 0, 255)
    rgb = np.dstack((red, green, blue)).astype(np.uint8)
    buffer = io.BytesIO()
    Image.fromarray(rgb, 'RGB').save(buffer, 'JPEG', quality=int(quality))
    return ts_us, buffer.getvalue()


class CamdH264Source:
    """One H.264 subscription, delivered to `callback` on a reader thread.

    callback(payload, ts_us, keyframe, mono_ns, wall_ns) is invoked per frame.
    Exceptions raised inside the callback are reported through `failure`.
    """

    def __init__(self, name, callback, path=SOCKET_PATH):
        self.camera = CAMERA_NAMES.get(name, name)
        self.name = name
        self.callback = callback
        self.failure = None
        self.frames = 0
        self.last_frame = time.monotonic()
        self._stop = threading.Event()
        self._pending_frame = None
        early_stream=False
        for attempt in range(5):
            self._connection = _Connection(path, timeout=5.0)
            retry=False
            try:
                self._connection.send({'cmd': 'STREAM_H264', 'cam': self.camera, 'on': 1})
                first=self._connection.read_exact(1)
                # The shipped daemon has concurrent ACK and frame writers.
                # An early binary frame may contain an ACK inside its payload;
                # do not hand ambiguous bytes to a decoder. Retry negotiation
                # before exposing this source to the common recording gate.
                if first!=b'{':
                    early_stream=True;retry=True
                    raise CamdError('%s: camd sent binary data before its acknowledgement' % self.camera)
                reply=first+self._connection.read_line(max_bytes=4096)
                answer=json.loads(reply.decode())
                if not isinstance(answer,dict) or answer.get('ok') is not True:
                    reason=answer.get('error','') if isinstance(answer,dict) else 'invalid acknowledgement'
                    retry=early_stream and '推流' in str(reason)
                    raise CamdError('%s: camd refused the H.264 stream: %s' % (self.camera,reason))
                break
            except BaseException:
                self._connection.close()
                if not retry or attempt==4:raise
                time.sleep(.15)
        self.handshake_retries=attempt
        self.description = ('camd STREAM_H264 cam=%s socket=%s (Annex-B, SPS/PPS per IDR)'
                            % (self.camera, path))
        self._thread = threading.Thread(target=self._run, name='camd-' + self.camera, daemon=True)
        self._thread.start()

    def _read_item(self):
        first = self._connection.read_exact(1)
        if first == b'{':
            reply = first + self._connection.read_line(max_bytes=4096)
            try:
                answer = json.loads(reply.decode())
            except (ValueError, UnicodeDecodeError) as error:
                raise CamdError('%s: invalid stream acknowledgement' % self.camera) from error
            if not isinstance(answer, dict) or answer.get('ok') is not True:
                raise CamdError('%s: camd refused the H.264 stream: %s' %
                                (self.camera, answer.get('error') if isinstance(answer, dict) else reply[:160]))
            return None
        header = first + self._connection.read_exact(HEADER_BYTES - 1)
        magic, length, flags, _reserved, ts_us = HEADER.unpack(header)
        if magic != H264_MAGIC:
            raise CamdError('%s: bad frame magic 0x%08X' % (self.camera, magic))
        if not 0 < length <= 32 * 1024 * 1024:
            raise CamdError('%s: implausible frame length %d' % (self.camera, length))
        payload = self._connection.read_exact(length)
        return payload, ts_us, bool(flags & KEYFRAME_FLAG)

    def _run(self):
        try:
            while not self._stop.is_set():
                item, self._pending_frame = self._pending_frame, None
                if item is None:
                    item = self._read_item()
                if item is None:
                    continue
                payload, ts_us, keyframe = item
                if not self.frames and not keyframe:
                    raise CamdError('%s: initial H.264 frame is not an IDR' % self.camera)
                mono, wall = time.monotonic_ns(), time.time_ns()
                self.frames += 1
                self.last_frame = time.monotonic()
                self.callback(payload, ts_us, keyframe, mono, wall)
        except Exception as error:  # surfaced via health()
            if not self._stop.is_set():
                self.failure = error

    def health(self):
        if self.failure is not None:
            raise CamdError('%s: %s' % (self.camera, self.failure))

    def close(self):
        self._stop.set()
        try:
            self._connection.send({'cmd': 'STREAM_H264', 'cam': self.camera, 'on': 0})
        except (OSError, CamdError):
            pass  # closing the socket also stops the subscription
        self._connection.close()
        self._thread.join(5)
        if self._thread.is_alive():
            raise RuntimeError('%s: camd stream reader did not stop' % self.camera)
