"""Pi-only MPP video backend; software fallback retains the original checks."""
from contextlib import contextmanager
import json
import logging
from pathlib import Path
import subprocess
import tempfile
import threading
import time

import numpy as np

from embodied_brain_collect.checkers import checks


def _timestamps(indices, raw, fps):
    result = indices.astype(np.float64) / fps
    if raw is not None:
        valid = indices < raw.size
        result[valid] = raw[indices[valid]]
    return result


def decode_mpp(mp4, frame_ts, window, rate_hz=2, *, python=None, timeout=120, black_lum_thr=8):
    if not np.isfinite(rate_hz) or rate_hz <= 0:
        raise ValueError('Invalid sampling rate')
    raw = np.asarray(frame_ts, dtype=np.float64) if frame_ts is not None else None
    i0 = i1 = None
    if window is not None and raw is not None and raw.size:
        inside = np.flatnonzero((raw >= window['t0']) & (raw <= window['t1']))
        if inside.size:
            i0, i1 = int(inside[0]), int(inside[-1])
    base = Path(__file__).resolve().parents[3]
    python = str(python or base / '.venvs/ros/bin/python')
    worker = Path(__file__).with_name('mpp_video_worker.py')
    request = dict(video=str(Path(mp4).resolve()), i0=i0, i1=i1, rate_hz=float(rate_hz),
                   black_lum_thr=float(black_lum_thr))
    with tempfile.TemporaryDirectory(prefix='orangepi-mpp-qc-') as temp:
        output = Path(temp) / 'metrics.npz'
        process = subprocess.run([python, str(worker), '--output', str(output)],
            input=json.dumps(request).encode(), stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, timeout=timeout)
        if process.returncode:
            raise RuntimeError(process.stderr.decode(errors='replace').strip()[-1200:] or
                               'MPP worker exited {}'.format(process.returncode))
        with np.load(output, allow_pickle=False) as z:
            values = {k: z[k].copy() for k in z.files}
    fps = float(values['fps'])
    n_frames = int(values['n_frames'])
    if not np.isfinite(fps) or fps <= 0 or n_frames <= 0:
        raise RuntimeError('Invalid decoded frame count/rate')
    indices, all_indices = values['indices'], values['all_indices']
    expected_start = i0 if i0 is not None else 0
    expected_end = min(i1 + 1, n_frames) if i1 is not None else n_frames
    expected = np.arange(expected_start, max(expected_start, expected_end), dtype=np.int64)
    if not np.array_equal(all_indices, expected):
        raise RuntimeError('MPP frame coverage is incomplete')
    stride = max(1, int(round(fps / rate_hz)))
    if not np.array_equal(indices, expected[expected % stride == 0]):
        raise RuntimeError('MPP sample indices differ from original sampling grid')
    for key, length in [('lums', len(indices)), ('diffs', max(0, len(indices) - 1)),
                        ('all_lums', len(all_indices))]:
        if values[key].shape != (length,) or not np.isfinite(values[key]).all():
            raise RuntimeError('Invalid MPP metric array: ' + key)
    if values['black_mask'].dtype != np.bool_ or values['black_mask'].shape != (len(all_indices),):
        raise RuntimeError('Invalid full-frame black mask')
    out = checks.VideoDecode(file=Path(mp4).name, n_frames=n_frames, fps=fps, rate_hz=rate_hz,
        lums=values['lums'], diffs=values['diffs'], t_samp=_timestamps(indices, raw, fps),
        n_window=(i1 - i0 + 1) if i0 is not None else None, i0=i0, i1=i1,
        black_lums=values['all_lums'], black_t_samp=_timestamps(all_indices, raw, fps),
        black_rate_hz=fps, black_mask=values['black_mask'], black_lum_thr=float(values['black_lum_thr']))
    out.mpp_timing = {key: values[key].item() for key in
                      ('elapsed_seconds', 'peak_rss_kib', 'backend')}
    return out


@contextmanager
def video_backend(settings):
    mode = settings.get('qc_video_backend', 'cv2')
    if mode not in ('cv2', 'auto', 'mpp'):
        raise ValueError('Unknown QC video backend: ' + str(mode))
    traces = []
    original = checks.decode_video
    lock = threading.Lock()
    # At most two validated MPP instances; each retains its own address limit.
    lanes = max(1, min(2, int(settings.get('qc_mpp_workers', 1))))
    slot = threading.Semaphore(lanes)
    software_slot = threading.Semaphore(1)

    def wrapped(mp4, frame_ts, window, rate_hz=2):
        started = time.monotonic()
        detail = {'stream': Path(mp4).parent.name, 'file': Path(mp4).name}
        try:
            with slot:
                result = decode_mpp(mp4, frame_ts, window, rate_hz,
                    python=settings.get('qc_mpp_python'),
                    timeout=float(settings.get('qc_mpp_timeout_seconds', 120)),
                    black_lum_thr=float(settings.get('qc_mpp_black_lum_thr', 8)))
            detail.update(result.mpp_timing)
            return result
        except Exception as error:
            detail.update(backend='cv2', fallback_reason=str(error)[:1200])
            if mode == 'mpp':
                raise
            logging.getLogger(__name__).warning('MPP QC fallback for %s: %s', mp4, error)
            # Software decoders share the parent's address space. Serialize
            # fallback to preserve the earlier memory-safe single-decoder path.
            with software_slot:
                return original(mp4, frame_ts, window, rate_hz)
        finally:
            detail['wall_seconds'] = time.monotonic() - started
            with lock:
                traces.append(detail)

    if mode != 'cv2':
        checks.decode_video = wrapped
    try:
        yield traces
    finally:
        checks.decode_video = original
