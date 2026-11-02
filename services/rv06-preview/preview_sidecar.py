#!/usr/bin/env python3
"""Read-only, latest-frame ROS JPEG thumbnails for the RV06 terminal."""
import argparse
from collections import Counter
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO
import json
import os
from pathlib import Path
import re
import signal
from socketserver import TCPServer, ThreadingMixIn
import threading
import time
from urllib.parse import parse_qs, urlsplit
import uuid
from frame_stream import packet,begin
from reference_preview import ReferenceSource, ReferenceError
from dual_wrist_preview import DualWristSource, DualError
from headphone_manager import HeadphoneManager
from headphone_http import handle_headphones
from replay_runtime import ReplayRuntime
try:
    from wrist_preview_transport import LeaseWriter, Reader, DEFAULT_DIRECTORY as WRIST_DIRECTORY
except ImportError:
    LeaseWriter = Reader = None
    WRIST_DIRECTORY = None

VERSION = '1.6.0'
ROS_TOPICS = {
    'cam_ros_left': '/fisheye/left/image_raw/compressed',
    'cam_ros_right': '/fisheye/right/image_raw/compressed',
    'cam_ros_bleft': '/fisheye/bleft/image_raw/compressed',
    'cam_ros_bright': '/fisheye/bright/image_raw/compressed',
}
WRISTS = ('cam_left_wrist', 'cam_right_wrist')
CAMERAS = tuple(ROS_TOPICS) + WRISTS
# EgoRecord v0.1.10 retired seeker_cam_bridge, so /fisheye/*/image_raw/compressed
# no longer exists and a ROS subscription for a headband camera can never yield a
# frame. The collector's preview worker publishes those cameras as JPEG files in
# its publisher directory, exactly like the wrist cameras, so the file source
# serves both kinds now and ROS_TOPICS is kept only to name the cameras.
FILE_CAMERAS = WRISTS + tuple(ROS_TOPICS)
MAX_INPUT = 2 * 1024 * 1024
MAX_OUTPUT = 128 * 1024
MAX_DIMENSION = 2048
MAX_PIXELS = 3 * 1024 * 1024
MAX_WAIT_MS = 150
STALE_SECONDS = 2.
IDLE_SECONDS = 10.
FRAME_PATTERN = re.compile(r'^[A-Za-z0-9_-]{1,48}:[0-9]{1,20}$')


def encode_phone_preview(raw, quality='fast'):
    """Display-only JPEG profiles; source mode preserves exact camera bytes."""
    if quality == 'source':return raw
    if quality not in ('fast','balanced'):raise ValueError('Unknown phone quality')
    from PIL import Image
    bound=(960,600) if quality=='fast' else (1280,800)
    with Image.open(BytesIO(raw)) as image:
        if (image.format!='JPEG' or max(image.size)>MAX_DIMENSION or
                image.width*image.height>MAX_PIXELS):raise ValueError('Invalid source JPEG')
        scale=min(1.,bound[0]/image.width,bound[1]/image.height)
        target=(max(1,int(image.width*scale)),max(1,int(image.height*scale)))
        # Match the source aspect ratio before scaled IDCT. A 1920x1080 JPEG
        # needs (960,540); requesting (960,600) forces a full-size decode.
        image.draft('RGB',target)
        image.thumbnail(target,getattr(Image,'Resampling',Image).BILINEAR)
        output=BytesIO()
        if image.mode!='RGB':image=image.convert('RGB')
        image.save(output,'JPEG',quality=82,subsampling=2,optimize=False)
        return output.getvalue()


def encode_thumbnail(raw):
    """Scaled JPEG IDCT; no full-resolution RGB image for ordinary JPEG input."""
    from PIL import Image
    if not 0 < len(raw) <= MAX_INPUT:
        raise ValueError('JPEG size exceeds limit')
    with Image.open(BytesIO(raw)) as image:
        if image.format != 'JPEG':
            raise ValueError('Not JPEG')
        width, height = image.size
        if (not 0 < width <= MAX_DIMENSION or not 0 < height <= MAX_DIMENSION or
                width * height > MAX_PIXELS):
            raise ValueError('JPEG dimensions exceed limit')
        if image.mode not in ('RGB', 'L'):
            raise ValueError('Unsupported JPEG color mode')
        # Camera JPEGs are baseline. Refuse coefficient-heavy progressive input.
        if image.info.get('progressive') or image.info.get('progression'):
            raise ValueError('Progressive JPEG is unsupported')
        image.draft('RGB', (480, 270))
        # Ubuntu 20.04 / ROS Noetic ships Pillow 7, before Image.Resampling.
        resampling = getattr(Image, 'Resampling', Image)
        image.thumbnail((480, 270), resampling.BILINEAR)
        output = BytesIO()
        if image.mode == 'RGB':
            image.save(output, 'JPEG', quality=65, subsampling=2,
                       progressive=False, optimize=False)
        else:
            with image.convert('RGB') as rgb:
                rgb.save(output, 'JPEG', quality=65, subsampling=2,
                         progressive=False, optimize=False)
        encoded = output.getvalue()
        if len(encoded) > MAX_OUTPUT:
            raise ValueError('Encoded thumbnail exceeds limit')
        return encoded


class LegacyWristFileSource:
    """Only read existing publisher files; never open a UVC device.

    Serves the headband cameras too: since the ROS bridge was retired they are
    published as files in the same controller directory.
    """
    def __init__(self, preview_dir=None, preview_root='/dev/shm'):
        self.preview_dir = Path(preview_dir) if preview_dir else None
        self.preview_root = Path(preview_root)
        self.last_key = {}

    def reset(self):
        self.last_key = {}

    def read(self, name):
        if name not in FILE_CAMERAS:
            return None
        directories = ([self.preview_dir] if self.preview_dir else
                       list(self.preview_root.glob('orangepi-preview-*')))
        candidates = []
        wall = time.time()
        for directory in directories:
            try:
                path = directory / (name + '.jpg')
                stat = path.stat()
                if 0 <= wall - stat.st_mtime < STALE_SECONDS:
                    candidates.append(path)
            except OSError:
                pass
        # An ambiguous set of active controller directories is not a valid source.
        if len(candidates) != 1:
            return None
        try:
            with candidates[0].open('rb') as handle:
                stat = os.fstat(handle.fileno())
                age = time.time() - stat.st_mtime
                if not 0 <= age < STALE_SECONDS or not 0 < stat.st_size <= MAX_INPUT:
                    return None
                key = (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size)
                # Keyed per camera: one shared slot would make the cameras
                # suppress each other's frames now that six of them are served.
                if key == self.last_key.get(name):
                    return None
                raw = handle.read(MAX_INPUT + 1)
            if not 0 < len(raw) <= MAX_INPUT:
                return None
            self.last_key[name] = key
            return raw, key, time.monotonic() - age
        except OSError:
            return None


class WristFileSource:
    """Demand-driven high-rate owner feed, with legacy file compatibility."""
    def __init__(self, preview_dir=None, preview_root='/dev/shm', ipc_directory=None):
        self.legacy = LegacyWristFileSource(preview_dir, preview_root)
        self.ipc_directory = ipc_directory
        self.lock = threading.Lock()
        self.lease = None
        self.reader = None
        self.camera = None
        self.epoch = None
        self.last_fast = 0.

    def reset(self):
        with self.lock:
            if self.lease is not None:
                self.lease.close()
            self.lease = self.reader = self.camera = self.epoch = None
            self.last_fast = 0.
            self.legacy.reset()

    def read(self, name):
        if name not in FILE_CAMERAS:
            return None
        if name not in WRISTS:
            # The owner lease transport only publishes wrist cameras. Headband
            # frames exist solely as files, so read them directly instead of
            # selecting a lease that would never carry them. The source key is
            # tagged like the wrist path so duplicate detection behaves alike.
            with self.lock:
                value = self.legacy.read(name)
            return ((value[0], ('legacy',) + value[1], value[2])
                    if value is not None else None)
        with self.lock:
            if self.ipc_directory is not None and LeaseWriter is not None:
                try:
                    if self.camera != name or self.lease is None:
                        if self.lease is not None:
                            self.lease.close()
                        self.lease = LeaseWriter(self.ipc_directory)
                        self.reader = Reader(self.ipc_directory)
                        self.epoch = self.lease.select(name)
                        self.camera = name
                        self.last_fast = 0.
                        self.legacy.reset()
                    self.lease.refresh()
                    value = self.reader.read(name, self.epoch)
                    if value is not None:
                        self.last_fast = time.monotonic()
                        return value[0], ('owner',) + value[1], value[2]
                    # Do not interleave old low-rate JPEGs between live owner
                    # frames, which would inflate FPS and show stale content.
                    if self.last_fast and time.monotonic()-self.last_fast < 1.:
                        return None
                except (OSError, ValueError, RuntimeError):
                    pass
            value = self.legacy.read(name)
            return (value[0], ('legacy',) + value[1], value[2]) if value is not None else None


class PreviewCore:
    """One selected source, latest raw frame and latest encoded frame only."""
    def __init__(self, subscribe=None, fallback=None, decoder=encode_thumbnail,
                 stale_seconds=STALE_SECONDS, idle_seconds=IDLE_SECONDS,
                 max_output=MAX_OUTPUT, max_fps=None):
        self.subscribe = subscribe
        self.fallback = fallback
        self.decoder = decoder
        self.max_output = max_output
        self.max_fps = max_fps
        self.encode_ms=0.
        self.stale_seconds = stale_seconds
        self.idle_seconds = idle_seconds
        self.boot = uuid.uuid4().hex
        self.started = time.monotonic()
        self.condition = threading.Condition(threading.Lock())
        self.selection_lock = threading.Lock()
        self.selected = None
        self.generation = 0
        self.serial = 0
        self.raw = None       # generation, serial, arrival, JPEG bytes
        self.encoded = None   # generation, serial, source arrival, JPEG bytes
        self.source_key = None
        self.attempted = None
        self.last_request = 0.
        self.subscription = None
        self.stopping = False
        self.counters = Counter()
        self.thread = threading.Thread(target=self._decode_loop,
                                       name='rv06-thumbnail', daemon=True)
        self.thread.start()

    @staticmethod
    def _unsubscribe(subscription):
        if subscription is not None:
            try:
                subscription.unregister()
            except Exception:
                pass

    def _clear_locked(self):
        self.generation += 1
        self.raw = self.encoded = self.source_key = self.attempted = None
        self.condition.notify_all()

    def select(self, name):
        if name not in CAMERAS:
            raise ValueError('Unknown camera')
        with self.selection_lock:
            with self.condition:
                if self.stopping:
                    raise RuntimeError('Stopping')
                self.last_request = time.monotonic()
                if self.selected == name:
                    return self.generation
                old, self.subscription = self.subscription, None
                self._clear_locked()
                self.selected = name
                generation = self.generation
                self.counters['switches'] += 1
            self._unsubscribe(old)
            if self.fallback:
                self.fallback.reset()
            # Headband cameras are file-fed since the ROS bridge was retired, so
            # only subscribe for a camera that is not served from files.
            if (name in ROS_TOPICS and name not in FILE_CAMERAS
                    and self.subscribe is not None):
                try:
                    self.subscription = self.subscribe(name, generation)
                except Exception:
                    with self.condition:
                        self.counters['subscribe_errors'] += 1
                        self.selected = None
                        self._clear_locked()
                    raise RuntimeError('ROS subscription unavailable')
            return generation

    def offer(self, name, generation, payload, source_key=None, arrival=None):
        """ROS callback: no decode, IO, blocking lock or frame-history queue."""
        if (not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_INPUT or
                payload[:2] != b'\xff\xd8'):
            self.counters['input_rejected'] += 1
            return False
        if not self.condition.acquire(False):
            self.counters['callback_lock_drops'] += 1
            return False
        try:
            if self.stopping or name != self.selected or generation != self.generation:
                return False
            if source_key is not None and source_key == self.source_key:
                self.counters['duplicate_messages'] += 1
                return False
            if self.raw is not None and (self.raw[0], self.raw[1]) != self.attempted:
                self.counters['replaced_raw_frames'] += 1
            self.serial += 1
            self.raw = (generation, self.serial,
                        time.monotonic() if arrival is None else arrival, payload)
            self.source_key = source_key
            self.counters['received_frames'] += 1
            self.condition.notify_all()
            return True
        finally:
            self.condition.release()

    def frame(self, name, generation, after='', wait_ms=150):
        deadline = time.monotonic() + min(MAX_WAIT_MS, max(0, wait_ms)) / 1000.
        with self.condition:
            while True:
                now = time.monotonic()
                if self.stopping or name != self.selected or generation != self.generation:
                    return 503, None, None
                self.last_request=now
                current = self.encoded
                fresh = (current is not None and current[0] == generation and
                         0 <= now - current[2] < self.stale_seconds)
                if fresh:
                    frame_id = self.boot + ':' + str(current[1])
                    if frame_id != after:
                        return 200, current[3], frame_id
                left = deadline - now
                if left <= 0:
                    return (304, None, frame_id) if fresh else (503, None, None)
                self.condition.wait(left)

    def _expire_idle(self):
        if not self.selection_lock.acquire(False):
            return
        old = None
        released = False
        try:
            with self.condition:
                if (self.selected is not None and
                        time.monotonic() - self.last_request >= self.idle_seconds):
                    old, self.subscription = self.subscription, None
                    self.selected = None
                    self._clear_locked()
                    self.counters['idle_releases'] += 1
                    released = True
            self._unsubscribe(old)
            if released and self.fallback:
                self.fallback.reset()
        finally:
            self.selection_lock.release()

    def release(self):
        """Release the single-camera demand while the dual calibration is open."""
        with self.selection_lock:
            with self.condition:
                if self.selected is None:
                    return
                old, self.subscription = self.subscription, None
                self.selected = None
                self._clear_locked()
            self._unsubscribe(old)
            if self.fallback:
                self.fallback.reset()

    def _decode_loop(self):
        next_decode = 0.
        next_file_poll = 0.
        while True:
            self._expire_idle()
            with self.condition:
                if self.stopping:
                    return
                name, generation = self.selected, self.generation
            now = time.monotonic()
            if name in FILE_CAMERAS and self.fallback and now >= next_file_poll:
                # Serialise file-feed selection with switch/close. Otherwise a
                # read from an old wrist snapshot could recreate its lease just
                # after reset had released it for ROS or shutdown.
                with self.selection_lock:
                    with self.condition:
                        valid = (not self.stopping and name == self.selected and
                                 generation == self.generation)
                    if valid:
                        next_file_poll = now + .01
                        value = self.fallback.read(name)
                        if value is not None:
                            raw, key, arrival = value
                            self.offer(name, generation, raw, key, arrival)
            with self.condition:
                if self.stopping:
                    return
                current = self.raw
                now = time.monotonic()
                ready = (current is not None and
                         (current[0], current[1]) != self.attempted and
                         current[0] == self.generation and now >= next_decode)
                if not ready:
                    pending = (current is not None and
                               (current[0], current[1]) != self.attempted)
                    wait = min(.1, max(.001, next_decode - now)) if pending else .1
                    if name in FILE_CAMERAS and self.fallback:
                        wait = min(wait, max(.0005, next_file_poll-now))
                    self.condition.wait(wait)
                    continue
                self.attempted = (current[0], current[1])
                # The owner still captures wrists at 30 Hz. Slight processing
                # headroom avoids an exact-rate limiter dropping jittered input.
                next_decode = now + 1. / (self.max_fps or (40. if name in WRISTS else 30.))
            if time.monotonic() - current[2] >= self.stale_seconds:
                with self.condition:
                    if self.raw is current:
                        self.raw = None
                continue
            try:
                encode_started=time.monotonic()
                encoded = self.decoder(current[3])
                self.encode_ms=(time.monotonic()-encode_started)*1000
                if not isinstance(encoded, bytes) or not 0 < len(encoded) <= self.max_output:
                    raise ValueError('Invalid thumbnail')
            except Exception:
                with self.condition:
                    if self.raw is current:
                        self.raw = None
                    self.counters['decode_errors'] += 1
                continue
            with self.condition:
                if self.raw is current:
                    self.raw = None
                if not self.stopping and current[0] == self.generation:
                    self.encoded = (current[0], current[1], current[2], encoded)
                    self.counters['encoded_frames'] += 1
                    self.condition.notify_all()

    def health(self):
        with self.condition:
            return {'version': VERSION,
                    'uptime_seconds': round(time.monotonic() - self.started, 1),
                    'selected_source': self.selected is not None,
                    'raw_slots': int(self.raw is not None),
                    'encoded_slots': int(self.encoded is not None),
                    'encode_ms': round(self.encode_ms, 2),
                    'counters': dict(self.counters)}

    def close(self):
        with self.selection_lock:
            with self.condition:
                self.stopping = True
                old, self.subscription = self.subscription, None
                self.selected = None
                self._clear_locked()
            self._unsubscribe(old)
            if self.fallback:
                self.fallback.reset()
        self.thread.join(3.)


class PreviewHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 2

    def server_bind(self):
        # Both supported bind addresses are numeric. Reverse DNS can stall
        # startup on the isolated Pi/RV06 link and is unnecessary for HTTP.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def __init__(self, address, core, token='', reference=None, dual=None, headphones=None, replay_runtime=None, phone_core=None):
        self.core = core
        self.phone_core = phone_core
        self.phone_profile_lock = threading.Lock()
        self.token = token
        self.reference = reference or ReferenceSource()
        self.dual = dual
        self.headphones = headphones
        self.replay_runtime = replay_runtime
        self.replay = None
        self.slots = threading.BoundedSemaphore(3)
        self.stream_slot=threading.BoundedSemaphore(2)
        self.overloaded = 0
        super().__init__(address, PreviewHandler)

    def process_request(self, request, address):
        if not self.slots.acquire(False):
            self.overloaded += 1
            try:
                # Drain a small GET header before closing. Closing a socket with
                # unread input can reset it and discard the intended 503 reply.
                request.settimeout(.05)
                incoming = b''
                deadline = time.monotonic() + .1
                while (b'\r\n\r\n' not in incoming and len(incoming) < 8192 and
                       time.monotonic() < deadline):
                    try:
                        part = request.recv(min(4096, 8192 - len(incoming)))
                    except OSError:
                        break
                    if not part:
                        break
                    incoming += part
                request.sendall(b'HTTP/1.0 503 Service Unavailable\r\n'
                                b'Content-Length: 0\r\nConnection: close\r\n\r\n')
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


class PreviewHandler(BaseHTTPRequestHandler):
    server_version = 'RV06Preview/' + VERSION
    sys_version = ''

    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(2.)

    def send(self, status, body=b'', content='application/json', frame_id=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, separators=(',', ':')).encode('ascii')
        try:
            self.send_response(status)
            self.send_header('Content-Type', content)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.send_header('X-Content-Type-Options', 'nosniff')
            if frame_id is not None:
                self.send_header('X-Preview-Frame', frame_id)
            self.end_headers()
            if body:
                self.wfile.write(body)
        except (OSError, TimeoutError):
            pass

    def do_POST(self):
        if not handle_headphones(self, 'POST'):
            self.send(404)

    def do_GET(self):
        if handle_headphones(self, 'GET'):
            return
        if len(self.path) > 1024:
            self.send(414)
            return
        url = urlsplit(self.path)
        if url.path == '/health':
            value = self.server.core.health()
            value['reference_detail'] = True
            value['headphone_management'] = self.server.headphones is not None
            value['recording_replay'] = (self.server.replay_runtime.health()
                                         if self.server.replay_runtime is not None else dict(enabled=False))
            value['reference_zooms'] = [1, 2, 4, 8, 16]
            value['http_overloaded'] = self.server.overloaded
            value['phone_source_preview'] = self.server.phone_core is not None
            value['phone_continuous_source']=True
            if self.server.phone_core is not None:
                value['phone_preview'] = self.server.phone_core.health()
            if self.server.dual is not None:
                value['dual_wrist'] = self.server.dual.health()
            self.send(200, value)
            return
        if self.server.token:
            token = self.headers.get('X-Access-Token', '')
            try:
                authorised = (len(token) <= 511 and
                              hmac.compare_digest(token.encode('ascii'),
                                                  self.server.token.encode('ascii')))
            except UnicodeEncodeError:
                authorised = False
            if not authorised:
                self.send(401)
                return
        if url.path.startswith('/phone/stream/'):
            name=url.path[len('/phone/stream/'):];query=parse_qs(url.query,keep_blank_values=True);quality=query.get('quality',['fast'])[0];core=self.server.phone_core
            if core is None or name not in {n+'.jpg' for n in CAMERAS} or set(query)-{'quality'} or any(len(v)!=1 for v in query.values()) or quality not in ('fast','balanced','source'):self.send(400);return
            if not self.server.stream_slot.acquire(False):self.send(429);return
            try:
                with self.server.phone_profile_lock:
                    if getattr(core,'phone_quality','fast')!=quality:core.release();core.phone_quality=quality
                    generation=core.select(name[:-4])
                begin(self);after='';deadline=time.monotonic()+20;last_sent=time.monotonic()
                while time.monotonic()<deadline:
                    status,body,frame=core.frame(name[:-4],generation,after,150)
                    if status==304:continue
                    if status==503:
                        with core.condition:valid=core.selected==name[:-4] and core.generation==generation
                        if not valid or time.monotonic()-last_sent>3:break
                        time.sleep(.02);continue
                    if status!=200:break
                    self.wfile.write(packet(dict(frame_id=frame,server_ms=round(core.encode_ms,2)),body));after=frame;last_sent=time.monotonic()
            except (OSError,ValueError,RuntimeError):pass
            finally:self.server.stream_slot.release()
            return
        if url.path.startswith('/phone/preview/'):
            core = self.server.phone_core
            name = url.path[len('/phone/preview/'):]
            if core is None:
                self.send(503); return
            if name == 'release' and not url.query:
                core.release(); self.send(200,{'released':True}); return
            if not name.endswith('.jpg') or name[:-4] not in CAMERAS:
                self.send(404); return
            query = parse_qs(url.query,keep_blank_values=True)
            try:
                if set(query)-{'after','wait_ms','quality'} or any(len(v)!=1 for v in query.values()):raise ValueError()
                after = query.get('after',[''])[0]
                wait = int(query.get('wait_ms',['150'])[0])
                quality=query.get('quality',['fast'])[0]
                if quality not in ('fast','balanced','source'):raise ValueError()
                if not 0 <= wait <= MAX_WAIT_MS or (after and not FRAME_PATTERN.fullmatch(after)):raise ValueError()
                with self.server.phone_profile_lock:
                    if getattr(core,'phone_quality','fast')!=quality:
                        core.release();core.phone_quality=quality
                    generation = core.select(name[:-4])
                    status,body,frame_id = core.frame(name[:-4],generation,after,wait)
                self.send(status,body or b'','image/jpeg' if status==200 else 'application/json',frame_id)
            except (ValueError,RuntimeError):self.send(503)
            return
        if url.path.startswith('/replay/'):
            if (not self.server.token and self.client_address[0] not in
                    ('127.0.0.1', '::1', '192.168.250.1', '192.168.250.2')):
                self.send(403, {'error': 'peer_forbidden'})
                return
            if self.server.replay_runtime is None:
                self.send(503, {'error': '回看服务尚未就绪'})
                return
            try:
                self.server.replay = self.server.replay_runtime.backend()
                from recording_replay import handle_replay
                if not handle_replay(self):
                    self.send(404)
            except Exception:
                self.send(503, {'error': '回看存储或服务不可用，请检查采集SD卡'})
            return
        if url.path.startswith('/calibration/wrist/'):
            match = re.fullmatch(r'/calibration/wrist/(cam_left_wrist|cam_right_wrist)\.jpg', url.path)
            if not match or self.server.dual is None:
                self.send(404)
                return
            query = parse_qs(url.query, keep_blank_values=True)
            if set(query)-{'after','wait_ms'} or any(len(value)!=1 for value in query.values()):
                self.send(400)
                return
            try:
                wait_ms = int(query.get('wait_ms', ['0'])[0])
                if not 0 <= wait_ms <= 50:
                    raise ValueError()
                self.server.core.release()
                status, body, frame_id = self.server.dual.get(match.group(1), self.headers.get('X-Access-Token', ''),
                                                            query.get('after', [''])[0], wait_ms)
            except ValueError:
                self.send(400)
                return
            except DualError as error:
                self.send(error.status, {'error': error.message})
                return
            except OSError:
                self.send(503, {'error': 'Wrist calibration transport unavailable'})
                return
            self.send(status, body or b'', 'image/jpeg' if status == 200 else 'application/json', frame_id)
            return
        if url.path == '/reference.jpg':
            detail = None
            if url.query not in ('', 'view=large'):
                # Exact fixed fields only. Do not let parse_qs discard empty
                # fields, decode aliases, or silently combine duplicates.
                fields = [field.split('=') for field in url.query.split('&')]
                if (len(fields) != 4 or any(len(field) != 2 for field in fields) or
                        set(field[0] for field in fields) != {'view', 'zoom', 'cx', 'cy'}):
                    self.send(400, {'error': 'Reference query unsupported'})
                    return
                query = dict(fields)
                if (query['view'] != 'detail' or query['zoom'] not in ('1', '2', '4', '8', '16') or
                        not re.fullmatch(r'[0-9]{1,5}', query['cx']) or
                        not re.fullmatch(r'[0-9]{1,5}', query['cy']) or
                        int(query['cx']) > 10000 or int(query['cy']) > 10000):
                    self.send(400, {'error': 'Reference query unsupported'})
                    return
                detail = (int(query['zoom']), int(query['cx']), int(query['cy']))
            try:
                if detail is not None:
                    body, frame_id = self.server.reference.get_detail(self.headers.get('X-Access-Token', ''), *detail)
                elif url.query == 'view=large':
                    body, frame_id = self.server.reference.get(self.headers.get('X-Access-Token', ''), large=True)
                else:
                    body, frame_id = self.server.reference.get(self.headers.get('X-Access-Token', ''))
            except ReferenceError as exc:
                self.send(exc.status, {'error': exc.message})
                return
            self.send(200, body, 'image/jpeg', frame_id)
            return
        match = re.fullmatch(r'/preview/([a-z_]+)\.jpg', url.path)
        if not match or match.group(1) not in CAMERAS:
            self.send(404)
            return
        query = parse_qs(url.query, keep_blank_values=True)
        after = query.get('after', [''])
        wait = query.get('wait_ms', ['150'])
        if (len(after) != 1 or len(wait) != 1 or
                (after[0] and not FRAME_PATTERN.fullmatch(after[0]))):
            self.send(400)
            return
        try:
            wait_ms = int(wait[0])
            if not 0 <= wait_ms <= MAX_WAIT_MS:
                raise ValueError()
        except ValueError:
            self.send(400)
            return
        try:
            name = match.group(1)
            if self.server.dual is not None:
                self.server.dual.release()
            generation = self.server.core.select(name)
            status, body, frame_id = self.server.core.frame(name, generation,
                                                           after[0], wait_ms)
        except (ValueError, RuntimeError):
            self.send(503)
            return
        self.send(status, body or b'', 'image/jpeg' if status == 200 else 'application/json',
                  frame_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bind', default='127.0.0.1',
                        choices=('127.0.0.1', '192.168.250.1'))
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--token-file', type=Path)
    parser.add_argument('--preview-dir', type=Path)
    parser.add_argument('--preview-root', type=Path, default=Path('/dev/shm'))
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('Invalid port')
    token = ''
    if args.token_file:
        with args.token_file.open('rb') as handle:
            token = handle.read(513)
        if not 24 <= len(token) <= 512:
            parser.error('Invalid token file')
        try:
            token = token.decode('ascii').strip()
        except UnicodeDecodeError:
            parser.error('Invalid token file')
        if not 24 <= len(token) <= 511 or '\r' in token or '\n' in token:
            parser.error('Invalid token file')
    from PIL import Image
    # Every camera is file-fed since EgoRecord v0.1.10 retired seeker_cam_bridge,
    # and the ROS master went with it. rospy.init_node() blocked here forever
    # ("master may not be running yet"), so the sidecar never reached serve_forever
    # and every preview request failed to connect. No subscriber is created now,
    # so ROS is not imported at all.
    core = PreviewCore(subscribe=None,
                       fallback=WristFileSource(args.preview_dir, args.preview_root,
                                                ipc_directory=WRIST_DIRECTORY))
    phone_core = None
    # Separate demand and latest-only slots preserve the RV06 thumbnail API.
    # Source mode forwards exact compressed bytes; display profiles reduce bandwidth.
    phone_core = PreviewCore(subscribe=None,
        fallback=WristFileSource(args.preview_dir,args.preview_root,ipc_directory=WRIST_DIRECTORY),
        decoder=lambda raw:encode_phone_preview(raw,getattr(phone_core,'phone_quality','fast')),
        max_output=MAX_INPUT,max_fps=60.,idle_seconds=3.)
    phone_core.phone_quality='fast'
    dual = DualWristSource()
    headphones = HeadphoneManager(Path.cwd())
    replay_runtime = ReplayRuntime(Path.cwd(), token)
    server = PreviewHTTPServer((args.bind, args.port), core, token, dual=dual, headphones=headphones,
                               replay_runtime=replay_runtime,phone_core=phone_core)
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *unused: stopped.set())
    signal.signal(signal.SIGINT, lambda *unused: stopped.set())
    server.timeout = .2
    try:
        while not stopped.is_set():
            server.handle_request()
    finally:
        server.server_close()
        replay_runtime.close()
        headphones.close()
        core.close()
        phone_core.close()
        dual.close()


if __name__ == '__main__':
    main()
