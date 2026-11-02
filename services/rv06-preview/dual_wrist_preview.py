"""Two-source calibration thumbnails from existing capture owners, never UVC."""
from io import BytesIO
import json
import re
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler, build_opener

from reference_preview import _NoRedirect
from wrist_preview_transport import (DualLeaseWriter, DualReader, DUAL_DIRECTORY,
                                     DUAL_CAMERAS, MAX_INPUT)

MAX_OUTPUT = 64 * 1024
STALE_SECONDS = 2.
IDLE_SECONDS = 2.
FRAME_PATTERN = re.compile(r'^dw_[0-9a-f]{16}_[1-9][0-9]{0,9}:[1-9][0-9]{0,19}$')


class DualError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message
        super().__init__(message)


def _text(value, maximum):
    return (isinstance(value, str) and 0 < len(value.encode('utf-8')) <= maximum
            and not any(ord(c) < 32 or ord(c) == 127 for c in value))


def status_context(status):
    """This order is also implemented by the native client; never sort sources."""
    if not isinstance(status, dict) or status.get('state') != 'wrist_manual':
        raise DualError(503, 'Wrist manual calibration is not active')
    system = status.get('system', {})
    assignment = status.get('wrist_assignment')
    if not isinstance(system, dict) or not isinstance(assignment, dict):
        raise DualError(503, 'Wrist assignment context unavailable')
    boot, pid = system.get('boot_id'), system.get('controller_pid')
    session, preview = assignment.get('session_id'), assignment.get('preview_id')
    sources = assignment.get('sources')
    if (not _text(boot, 95) or type(pid) is not int or not 0 < pid <= 9007199254740991 or
            not _text(session, 64) or not _text(preview, 64) or status.get('preview_id') != preview or
            not isinstance(sources, dict) or set(sources) != set(DUAL_CAMERAS) or
            not all(_text(sources[name], 255) for name in DUAL_CAMERAS) or
            sources[DUAL_CAMERAS[0]] == sources[DUAL_CAMERAS[1]]):
        raise DualError(503, 'Wrist assignment context unavailable')
    return boot, str(pid), session, preview, sources[DUAL_CAMERAS[0]], sources[DUAL_CAMERAS[1]]


def context_fingerprint(context):
    value = 14695981039346656037
    for byte in '\n'.join(context).encode('utf-8'):
        value = ((value ^ byte) * 1099511628211) & 0xffffffffffffffff
    return '%016x' % value


def encode_calibration_thumbnail(raw):
    from PIL import Image
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_INPUT:
        raise ValueError('Calibration JPEG exceeds limit')
    with Image.open(BytesIO(raw)) as image:
        width, height = image.size
        if (image.format != 'JPEG' or image.mode not in ('RGB', 'L') or
                not 0 < width <= 2048 or not 0 < height <= 2048 or width * height > 3 * 1024 * 1024 or
                image.info.get('progressive') or image.info.get('progression')):
            raise ValueError('Calibration JPEG unsupported')
        image.draft('RGB', (240, 180))
        image.thumbnail((240, 180), getattr(Image, 'Resampling', Image).BILINEAR)
        output = BytesIO()
        if image.mode == 'RGB':
            image.save(output, 'JPEG', quality=65, subsampling=2, progressive=False, optimize=False)
        else:
            with image.convert('RGB') as rgb:
                rgb.save(output, 'JPEG', quality=65, subsampling=2, progressive=False, optimize=False)
        result = output.getvalue()
    if len(result) > MAX_OUTPUT:
        raise ValueError('Calibration thumbnail exceeds limit')
    return result


class DualWristSource:
    """One encoder, two latest raw/encoded slots, no history or extra captures."""
    def __init__(self, directory=DUAL_DIRECTORY, status_reader=None,
                 encoder=encode_calibration_thumbnail, idle_seconds=IDLE_SECONDS):
        self.http = build_opener(ProxyHandler({}), _NoRedirect())
        self.status_reader = status_reader or self._read_status
        self.encoder, self.idle_seconds = encoder, idle_seconds
        self.lease = DualLeaseWriter(directory)
        self.reader = DualReader(directory)
        self.condition = threading.Condition(threading.RLock())
        self.context = self.epoch = None
        self.raw, self.encoded = {}, {}
        self.last_request = 0.
        self.stopping = False
        self.errors = 0
        self.next_decode = {}
        self.thread = threading.Thread(target=self._encode_loop, name='rv06-dual-wrist', daemon=True)
        self.thread.start()

    def _read_status(self, token):
        request = Request('http://127.0.0.1:8765/api/wrist-context')
        if token:
            request.add_header('X-Access-Token', token)
        try:
            with self.http.open(request, timeout=2.) as response:
                raw = response.read(4096 + 1)
            if len(raw) > 4096:
                raise ValueError('Status size')
            return json.loads(raw)
        except HTTPError as error:
            raise DualError(401 if error.code in (401, 403) else 503,
                            'Wrist calibration authorization unavailable') from None
        except Exception:
            raise DualError(503, 'Wrist calibration status unavailable') from None

    def _context(self, token):
        try:
            return status_context(self.status_reader(token))
        except DualError:
            raise
        except Exception:
            raise DualError(503, 'Wrist assignment context unavailable') from None

    def _release_locked(self):
        self.lease.close(); self.reader.reset()
        self.context = self.epoch = None
        self.raw.clear(); self.encoded.clear(); self.next_decode.clear()
        self.condition.notify_all()

    def release(self):
        with self.condition:
            if self.context is not None:
                self._release_locked()

    def get(self, camera, token='', after='', wait_ms=0):
        if camera not in DUAL_CAMERAS:
            raise DualError(404, 'Unknown wrist calibration camera')
        if (not isinstance(after, str) or (after and not FRAME_PATTERN.fullmatch(after)) or
                type(wait_ms) is not int or not 0 <= wait_ms <= 50):
            raise DualError(400, 'Invalid wrist calibration query')
        try:
            context = self._context(token)
        except DualError:
            self.release()
            raise
        deadline = time.monotonic() + wait_ms / 1000.
        with self.condition:
            if self.stopping:
                raise DualError(503, 'Wrist calibration preview stopped')
            self.last_request = time.monotonic()
            if context != self.context:
                self._release_locked()
                self.epoch = self.lease.select(context[3], dict(zip(DUAL_CAMERAS, context[4:])))
                self.context = context
            else:
                self.lease.refresh()
            epoch = self.epoch
            self.condition.notify_all()
            while True:
                if self.stopping or self.context != context or self.epoch != epoch:
                    raise DualError(503, 'Wrist calibration context changed')
                frame = self.encoded.get(camera)
                fresh = frame is not None and 0 <= time.monotonic() - frame[2] < STALE_SECONDS
                if fresh and frame[0] != after:
                    result = 200, frame[1], frame[0]
                    break
                left = deadline - time.monotonic()
                if left <= 0:
                    if not fresh:
                        raise DualError(503, 'Wrist calibration frame unavailable')
                    result = 304, None, frame[0]
                    break
                self.condition.wait(left)
        try:
            current = self._context(token)
        except DualError:
            self.release()
            raise
        with self.condition:
            if current != context or self.context != context or self.epoch != epoch:
                if self.context == context:
                    self._release_locked()
                raise DualError(503, 'Wrist calibration context changed')
            if not 0 <= time.monotonic() - frame[2] < STALE_SECONDS:
                raise DualError(503, 'Wrist calibration frame expired')
        return result

    def _encode_loop(self):
        while True:
            with self.condition:
                if self.stopping:
                    return
                if self.context is None:
                    self.condition.wait(.05)
                    continue
                if time.monotonic() - self.last_request >= self.idle_seconds:
                    self._release_locked()
                    continue
            # Exactly one worker services both sources in a fixed alternating
            # order. At most two JPEG inputs and two outputs can be retained.
            for camera in DUAL_CAMERAS:
                try:
                    with self.condition:
                        if self.stopping or self.context is None:
                            break
                        context, epoch = self.context, self.epoch
                        now = time.monotonic()
                        if now < self.next_decode.get(camera, 0):
                            continue
                        value = self.reader.read(camera, epoch, context[3], dict(zip(DUAL_CAMERAS, context[4:])))
                        if value is None:
                            continue
                        raw, key, arrival = value
                        self.raw[camera] = raw
                        self.next_decode[camera] = now + 1./40.
                    result = self.encoder(raw)
                    if not isinstance(result, bytes) or not 0 < len(result) <= MAX_OUTPUT:
                        raise ValueError('Calibration thumbnail exceeds limit')
                    frame_id = 'dw_%s_%d:%d' % (context_fingerprint(context), key[1], key[2])
                    with self.condition:
                        self.raw.pop(camera, None)
                        if (self.context == context and self.epoch == epoch and
                                0 <= time.monotonic()-arrival < STALE_SECONDS):
                            self.encoded[camera] = frame_id, result, arrival
                            self.condition.notify_all()
                except Exception:
                    with self.condition:
                        self.raw.pop(camera, None)
                        self.errors += 1
            with self.condition:
                if self.stopping:
                    return
                self.condition.wait(.004)

    def health(self):
        with self.condition:
            return dict(selected=self.context is not None, raw_slots=len(self.raw),
                        encoded_slots=len(self.encoded), encoder_threads=1, errors=self.errors)

    def close(self):
        with self.condition:
            self.stopping = True
            self._release_locked()
        self.thread.join(3.)
