"""Authorized reference-image adapter. No caller-supplied filesystem paths."""
from io import BytesIO
from http.client import HTTPException
import json
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

UPSTREAM = 'http://127.0.0.1:8765'
MAX_STATUS = 512 * 1024
MAX_IMAGE = 64 * 1024 * 1024
MAX_OUTPUT = 128 * 1024
MAX_LARGE_OUTPUT = 256 * 1024
MAX_PIXELS = 20_000_000
MAX_DIMENSION = 16384
INTERMEDIATE_SIZE = (1280, 840)
SMALL_SIZE = (320, 210)
LARGE_SIZE = (760, 420)
DETAIL_SIZE = (760, 400)
DETAIL_ZOOMS = (1, 2, 4, 8, 16)


class ReferenceError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message
        super().__init__(message)


def _context(boot_id, controller_pid, image):
    if (not isinstance(image, str) or not image):
        raise ReferenceError(404, 'No reference image')
    if (not isinstance(boot_id, str) or not boot_id or
            type(controller_pid) is not int or not 0 < controller_pid <= 9007199254740991):
        raise ReferenceError(503, 'Reference context unavailable')
    try:
        boot_bytes, image_bytes = boot_id.encode('utf-8'), image.encode('utf-8')
    except UnicodeError:
        raise ReferenceError(503, 'Reference context unavailable') from None
    if (len(boot_bytes) > 95 or len(image_bytes) > 255 or
            any(ord(c) < 32 or ord(c) == 127 for c in boot_id + image)):
        raise ReferenceError(503, 'Reference context unavailable')
    return boot_id, controller_pid, image


def context_fingerprint(boot_id, controller_pid, image):
    value = _context(boot_id, controller_pid, image)
    raw = (value[0] + '\n' + str(value[1]) + '\n' + value[2]).encode('utf-8')
    result = 14695981039346656037
    for byte in raw:
        result = ((result ^ byte) * 1099511628211) & 0xffffffffffffffff
    return 'ref_%016x' % result


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def encode_reference(raw, max_size=SMALL_SIZE):
    from PIL import Image
    if (type(max_size) is not tuple or len(max_size) != 2 or
            any(type(value) is not int for value in max_size) or
            max_size not in (SMALL_SIZE, LARGE_SIZE)):
        raise ReferenceError(503, 'Reference view unsupported')
    output_limit = MAX_LARGE_OUTPUT if max_size == LARGE_SIZE else MAX_OUTPUT
    if not 0 < len(raw) <= MAX_IMAGE:
        raise ReferenceError(503, 'Reference image exceeds limit')
    image = None
    try:
        image = Image.open(BytesIO(raw))
        width, height = image.size
        if (image.format not in ('PNG', 'JPEG') or image.mode not in ('RGB', 'L', 'RGBA') or
                not 0 < width <= MAX_DIMENSION or not 0 < height <= MAX_DIMENSION or width * height > MAX_PIXELS):
            raise ReferenceError(503, 'Reference image unsupported')
        # JPEG uses a reduced IDCT before allocating its decoded pixels.
        image.draft('RGB', max_size)
        resampling = getattr(Image, 'Resampling', Image)
        width, height = image.size
        if width > INTERMEDIATE_SIZE[0] or height > INTERMEDIATE_SIZE[1]:
            ratio = min(INTERMEDIATE_SIZE[0] / width, INTERMEDIATE_SIZE[1] / height)
            size = (max(1, int(width * ratio)), max(1, int(height * ratio)))
            # BILINEAR on RGBA creates a full-size premultiplied copy in Pillow
            # 7. Use NEAREST for this first reduction, then release the original
            # before the small, quality-filtered resize to bound peak memory.
            reduced = image.resize(size, resampling.NEAREST)
            image.close()
            image = reduced
        image.thumbnail(max_size, resampling.BILINEAR)
        output = BytesIO()
        if image.mode == 'RGBA':
            with Image.new('RGB', image.size, (255, 255, 255)) as rgb:
                with image.getchannel('A') as alpha:
                    rgb.paste(image, mask=alpha)
                rgb.save(output, 'JPEG', quality=65, subsampling=2, progressive=False, optimize=False)
        elif image.mode != 'RGB':
            with image.convert('RGB') as rgb:
                rgb.save(output, 'JPEG', quality=65, subsampling=2, progressive=False, optimize=False)
        else:
            image.save(output, 'JPEG', quality=65, subsampling=2, progressive=False, optimize=False)
        encoded = output.getvalue()
    except ReferenceError:
        raise
    except Exception:
        raise ReferenceError(503, 'Reference image unsupported') from None
    finally:
        if image is not None:
            image.close()
    if len(encoded) > output_limit:
        raise ReferenceError(503, 'Reference thumbnail exceeds limit')
    return encoded


def _detail_parameters(zoom, cx, cy):
    if (type(zoom) is not int or zoom not in DETAIL_ZOOMS or
            type(cx) is not int or type(cy) is not int or
            not 0 <= cx <= 10000 or not 0 <= cy <= 10000):
        raise ReferenceError(503, 'Reference view unsupported')
    # A fit view has only one effective crop, regardless of requested center.
    return (zoom, 5000, 5000) if zoom == 1 else (zoom, cx, cy)


def _detail_jpeg(image):
    # Text and colored fine lines benefit from full chroma resolution. No
    # unbounded optimization work: at most these three fixed quality attempts.
    output = BytesIO()
    for quality in (88, 80, 72):
        output.seek(0)
        output.truncate()
        image.save(output, 'JPEG', quality=quality, subsampling=0, progressive=False, optimize=False)
        if output.tell() <= MAX_LARGE_OUTPUT:
            return output.getvalue()
    raise ReferenceError(503, 'Reference thumbnail exceeds limit')


def encode_reference_detail(raw, zoom=1, cx=5000, cy=5000):
    """Crop source pixels before reducing them; never zoom a thumbnail."""
    from PIL import Image
    zoom, cx, cy = _detail_parameters(zoom, cx, cy)
    if not 0 < len(raw) <= MAX_IMAGE:
        raise ReferenceError(503, 'Reference image exceeds limit')
    image = None
    try:
        image = Image.open(BytesIO(raw))
        width, height = image.size
        if (image.format not in ('PNG', 'JPEG') or image.mode not in ('RGB', 'L', 'RGBA') or
                not 0 < width <= MAX_DIMENSION or not 0 < height <= MAX_DIMENSION or
                width * height > MAX_PIXELS):
            raise ReferenceError(503, 'Reference image unsupported')
        ratio = min(DETAIL_SIZE[0] / width, DETAIL_SIZE[1] / height)
        target_size = (max(1, int(round(width * ratio))), max(1, int(round(height * ratio))))
        # In particular, do not call JPEG draft here: reduced IDCT pixels cannot
        # supply the original detail required by a high-zoom crop.
        if zoom != 1:
            crop_width = max(1, (width + zoom - 1) // zoom)
            crop_height = max(1, (height + zoom - 1) // zoom)
            left = max(0, min(width - crop_width,
                              (width * cx * 2 - crop_width * 10000 + 10000) // 20000))
            top = max(0, min(height - crop_height,
                             (height * cy * 2 - crop_height * 10000 + 10000) // 20000))
            cropped = image.crop((left, top, left + crop_width, top + crop_height))
            image.close()
            image = cropped
        resampling = getattr(Image, 'Resampling', Image)
        width, height = image.size
        if width > INTERMEDIATE_SIZE[0] or height > INTERMEDIATE_SIZE[1]:
            ratio = min(INTERMEDIATE_SIZE[0] / width, INTERMEDIATE_SIZE[1] / height)
            size = (max(1, int(width * ratio)), max(1, int(height * ratio)))
            # Keep the Pillow 7 RGBA bound, but only after selecting the ROI.
            reduced = image.resize(size, resampling.NEAREST)
            image.close()
            image = reduced
        # Every zoom occupies the same fitted original-aspect rectangle. A
        # small native ROI is intentionally enlarged for readable detail.
        if image.size != target_size:
            resized = image.resize(target_size, resampling.BILINEAR)
            image.close()
            image = resized
        if image.mode == 'RGBA':
            with Image.new('RGB', image.size, (255, 255, 255)) as rgb:
                with image.getchannel('A') as alpha:
                    rgb.paste(image, mask=alpha)
                encoded = _detail_jpeg(rgb)
        elif image.mode != 'RGB':
            with image.convert('RGB') as rgb:
                encoded = _detail_jpeg(rgb)
        else:
            encoded = _detail_jpeg(image)
    except ReferenceError:
        raise
    except Exception:
        raise ReferenceError(503, 'Reference image unsupported') from None
    finally:
        if image is not None:
            image.close()
    return encoded


class ReferenceSource:
    def __init__(self, upstream=UPSTREAM, timeout=2.0, opener=None, encoder=encode_reference, large_encoder=None,
                 detail_encoder=encode_reference_detail):
        # Only a loopback port can be injected for tests. Production has no URL
        # option and always uses the controller's fixed loopback API endpoints.
        parsed = urlsplit(upstream)
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.username or parsed.password or
                parsed.path not in ('', '/') or parsed.query or parsed.fragment or not 1 <= (parsed.port or 80) <= 65535):
            raise ValueError('Reference upstream must be loopback HTTP')
        self.upstream = upstream.rstrip('/')
        self.timeout = timeout
        self.opener = opener or build_opener(ProxyHandler({}), _NoRedirect())
        self.encoder = encoder
        self.large_encoder = large_encoder or (lambda raw: encode_reference(raw, LARGE_SIZE))
        self.detail_encoder = detail_encoder
        self.lock = Lock()
        # One context, two thumbnail JPEGs and the latest detail JPEG only.
        # Detail may retain one compressed source (<=64MiB) to avoid refetching it
        # on every released pan; decoded image objects and tokens never persist.
        self.cache = None
        self.large_cache = None
        self.detail_cache = None
        self.detail_raw_cache = None

    def _clear_cache(self):
        self.cache = self.large_cache = self.detail_cache = self.detail_raw_cache = None

    def _clear_changed_context(self, before):
        if any(cached and cached[0] != before for cached in
               (self.cache, self.large_cache, self.detail_cache, self.detail_raw_cache)):
            self._clear_cache()

    @staticmethod
    def _validate_token(token):
        if not isinstance(token, str) or len(token) > 511 or any(ord(c)<32 or ord(c)==127 for c in token):
            raise ReferenceError(401, 'Reference authorization failed')
        try:
            token.encode('ascii')
        except UnicodeError:
            raise ReferenceError(401, 'Reference authorization failed') from None

    def _fetch(self, path, token, limit):
        headers = {'Accept-Encoding': 'identity', 'Connection': 'close'}
        if token:
            headers['X-Access-Token'] = token
        request = Request(self.upstream + path, headers=headers, method='GET')
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise ReferenceError(503, 'Reference controller unavailable')
                length = response.headers.get('Content-Length')
                if length is not None:
                    try:
                        size = int(length)
                    except (TypeError, ValueError):
                        raise ReferenceError(503, 'Reference response invalid') from None
                    if not 0 <= size <= limit:
                        raise ReferenceError(503, 'Reference response exceeds limit')
                value = response.read(limit + 1)
                if len(value) > limit:
                    raise ReferenceError(503, 'Reference response exceeds limit')
                if length is not None and len(value) != size:
                    raise ReferenceError(503, 'Reference response invalid')
                return value
        except HTTPError as exc:
            status = exc.code
            exc.close()
            if status in (401, 403):
                raise ReferenceError(401, 'Reference authorization failed') from None
            if status == 404 and path == '/api/reference':
                raise ReferenceError(404, 'No reference image') from None
            raise ReferenceError(503, 'Reference controller unavailable') from None
        except ReferenceError:
            raise
        except (OSError, URLError, ValueError, TimeoutError, HTTPException):
            raise ReferenceError(503, 'Reference controller unavailable') from None

    def _status(self, token):
        try:
            status = json.loads(self._fetch('/api/status', token, MAX_STATUS).decode('utf-8'))
            if not isinstance(status, dict) or not isinstance(status.get('system'), dict):
                raise ReferenceError(503, 'Reference context unavailable')
            return _context(status['system'].get('boot_id'), status['system'].get('controller_pid'), status.get('image'))
        except ReferenceError:
            raise
        except (ValueError, UnicodeError, TypeError):
            raise ReferenceError(503, 'Reference response invalid') from None

    def get(self, token='', large=False):
        try:
            if type(large) is not bool:
                raise ReferenceError(503, 'Reference view unsupported')
            self._validate_token(token)
            before = self._status(token)  # Every request, including a cache hit, is authorized.
            if not self.lock.acquire(timeout=.2):
                raise ReferenceError(503, 'Reference decoder busy')
            try:
                self._clear_changed_context(before)
                cached = self.large_cache if large else self.cache
                if cached:
                    encoded = cached[1]
                else:
                    encoder = self.large_encoder if large else self.encoder
                    encoded = encoder(self._fetch('/api/reference', token, MAX_IMAGE))
                after = self._status(token)
                if before != after:
                    raise ReferenceError(503, 'Reference source changed')
                if large:
                    self.large_cache = (before, encoded)
                else:
                    self.cache = (before, encoded)
                return encoded, context_fingerprint(*before)
            finally:
                self.lock.release()
        except ReferenceError:
            self._clear_cache()
            raise
        except Exception:
            self._clear_cache()
            raise ReferenceError(503, 'Reference image unavailable') from None

    def get_detail(self, token='', zoom=1, cx=5000, cy=5000):
        try:
            variant = _detail_parameters(zoom, cx, cy)
            self._validate_token(token)
            before = self._status(token)  # Authorize even a JPEG/compressed cache hit.
            if not self.lock.acquire(timeout=.2):
                raise ReferenceError(503, 'Reference decoder busy')
            try:
                self._clear_changed_context(before)
                raw = None
                if self.detail_cache and self.detail_cache[1] == variant:
                    encoded = self.detail_cache[2]
                else:
                    raw = (self.detail_raw_cache[1] if self.detail_raw_cache else
                           self._fetch('/api/reference', token, MAX_IMAGE))
                    encoded = self.detail_encoder(raw, *variant)
                    if not isinstance(encoded, bytes) or not 0 < len(encoded) <= MAX_LARGE_OUTPUT:
                        raise ReferenceError(503, 'Reference thumbnail exceeds limit')
                after = self._status(token)
                if before != after:
                    raise ReferenceError(503, 'Reference source changed')
                self.detail_cache = (before, variant, encoded)
                if raw is not None:
                    self.detail_raw_cache = (before, raw)
                return encoded, context_fingerprint(*before)
            finally:
                self.lock.release()
        except ReferenceError:
            self._clear_cache()
            raise
        except Exception:
            self._clear_cache()
            raise ReferenceError(503, 'Reference image unavailable') from None
