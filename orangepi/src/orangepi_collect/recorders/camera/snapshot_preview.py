"""Idle-only camd preview without creating/destroying MPP decoder sessions.

Recording still uses CamdH264Source and H264JpegPreview, unchanged. This source
only reads SNAPSHOT and generates disposable viewer JPEGs on a bounded thread.
"""
import io
import threading
import time


def nv12_jpeg(width, height, payload, size, quality=70):
    import numpy as np
    from PIL import Image
    if width <= 0 or height <= 0 or width % 2 or height % 2:
        raise ValueError('Invalid NV12 geometry')
    if len(payload) != width * height * 3 // 2:
        raise ValueError('Truncated NV12 frame')
    out_width, out_height = min(width, size[0]), min(height, size[1])
    if min(out_width, out_height) <= 0:
        raise ValueError('Invalid preview size')
    raw = np.frombuffer(payload, dtype=np.uint8)
    y = raw[:width * height].reshape(height, width)
    uv = raw[width * height:].reshape(height // 2, width // 2, 2)
    rows = np.arange(out_height, dtype=np.intp) * height // out_height
    columns = np.arange(out_width, dtype=np.intp) * width // out_width
    pixels = np.empty((out_height, out_width, 3), dtype=np.uint8)
    pixels[:, :, 0] = y[rows[:, None], columns[None, :]]
    pixels[:, :, 1:] = uv[(rows // 2)[:, None], (columns // 2)[None, :]]
    output = io.BytesIO()
    # NV12 is Y plus interleaved Cb/Cr. libjpeg accepts YCbCr directly; no full
    # RGB matrix conversion or extra hardware encoder session is needed.
    Image.fromarray(pixels, 'YCbCr').save(output, 'JPEG', quality=quality)
    return output.getvalue()


class SnapshotPreviewSource:
    """The idle preview interface, with one request in flight per source."""
    def __init__(self, name, callback, width=1920, height=1200,
                 snapshot=None, encoder=nv12_jpeg, interval=1/30):
        if snapshot is None:
            from orangepi_collect.recorders.camera.camd_source import snapshot
        self.size = (width, height)
        self.preview = self  # preview_worker checks source.preview.size
        self.frames = 0
        self.dropped = 0
        self.error = None
        self.last_frame = time.monotonic()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run,
            args=(name, callback, snapshot, encoder, interval),
            name='snapshot-' + name, daemon=True)
        self.thread.start()

    def _run(self, name, callback, snapshot, encoder, interval):
        previous = None
        try:
            while not self.stop.is_set():
                started = time.monotonic()
                width, height, stamp, raw = snapshot(name, timeout=3.0)
                if self.stop.is_set():break
                if stamp != previous:
                    payload = encoder(width, height, raw, self.size)
                    if self.stop.is_set():break
                    callback(payload)
                    previous = stamp
                    self.frames += 1
                    self.last_frame = time.monotonic()
                if self.stop.wait(max(.002, interval - (time.monotonic() - started))):break
        except Exception as error:
            if not self.stop.is_set():self.error = str(error)

    def health(self):
        if self.error:raise RuntimeError('snapshot preview: ' + self.error)
        if time.monotonic() - self.last_frame > 4:
            raise RuntimeError('snapshot preview has no fresh frames for 4 seconds')

    def close(self):
        self.stop.set()
        self.thread.join(3.2)
        # A late/failed socket read can only retain this daemon thread, never a
        # hardware decoder or camd's exclusive H.264 recording subscription.
