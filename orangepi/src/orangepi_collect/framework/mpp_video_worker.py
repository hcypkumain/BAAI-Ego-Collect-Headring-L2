#!/usr/bin/env python3
"""Bounded MPP decode worker in the Pi's Python 3.8 GStreamer environment."""
import argparse
import ctypes
import ctypes.util
import json
from pathlib import Path
import resource
import subprocess
import sys
import time

import cv2
import numpy as np
import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstVideo', '1.0')
from gi.repository import Gst, GstVideo


class LegacyGray:
    """Use FFmpeg's YUV420P -> BGR conversion, matching the old OpenCV decoder."""
    def __init__(self):
        scale_path = ctypes.util.find_library('swscale')
        util_path = ctypes.util.find_library('avutil')
        if not scale_path or not util_path:
            raise RuntimeError('FFmpeg color-conversion libraries unavailable')
        self.scale = ctypes.CDLL(scale_path)
        util = ctypes.CDLL(util_path)
        util.av_get_pix_fmt.argtypes = [ctypes.c_char_p]
        util.av_get_pix_fmt.restype = ctypes.c_int
        self.source_format = util.av_get_pix_fmt(b'yuv420p')
        self.target_format = util.av_get_pix_fmt(b'bgr24')
        self.byte_pointer = ctypes.POINTER(ctypes.c_uint8)
        self.scale.sws_getContext.argtypes = [ctypes.c_int] * 7 + [ctypes.c_void_p] * 3
        self.scale.sws_getContext.restype = ctypes.c_void_p
        self.scale.sws_scale.argtypes = [ctypes.c_void_p, ctypes.POINTER(self.byte_pointer),
            ctypes.POINTER(ctypes.c_int), ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(self.byte_pointer), ctypes.POINTER(ctypes.c_int)]
        self.scale.sws_scale.restype = ctypes.c_int
        self.scale.sws_freeContext.argtypes = [ctypes.c_void_p]
        self.scale.sws_freeContext.restype = None
        self.context = None
        self.shape = None

    def convert(self, yuv):
        height, width = yuv.shape[0] * 2 // 3, yuv.shape[1]
        if self.shape != (height, width):
            self.close()
            self.context = self.scale.sws_getContext(width, height, self.source_format,
                width, height, self.target_format, 4, None, None, None)  # SWS_BICUBIC
            if not self.context:
                raise RuntimeError('FFmpeg color context allocation failed')
            self.shape = (height, width)
            self.bgr = np.empty((height, width, 3), np.uint8)
        u = np.ascontiguousarray(yuv[height:, 0::2])
        v = np.ascontiguousarray(yuv[height:, 1::2])
        pointers = (self.byte_pointer * 4)(
            yuv.ctypes.data_as(self.byte_pointer), u.ctypes.data_as(self.byte_pointer),
            v.ctypes.data_as(self.byte_pointer), self.byte_pointer())
        strides = (ctypes.c_int * 4)(width, width // 2, width // 2, 0)
        dest = (self.byte_pointer * 4)(self.bgr.ctypes.data_as(self.byte_pointer),
            self.byte_pointer(), self.byte_pointer(), self.byte_pointer())
        dest_strides = (ctypes.c_int * 4)(width * 3, 0, 0, 0)
        if self.scale.sws_scale(self.context, pointers, strides, 0, height, dest, dest_strides) != height:
            raise RuntimeError('FFmpeg color conversion returned incomplete image')
        return cv2.cvtColor(self.bgr, cv2.COLOR_BGR2GRAY)

    def close(self):
        if self.context:
            self.scale.sws_freeContext(self.context)
            self.context = None
        self.shape = None


def decode(request):
    Gst.init(None)
    cv2.setNumThreads(1)
    pipeline = Gst.Pipeline.new('qc-mpp')
    source = Gst.ElementFactory.make('filesrc', 'source')
    demux = Gst.ElementFactory.make('qtdemux', 'demux')
    probe=json.loads(subprocess.check_output(['ffprobe','-v','error','-select_streams','v:0',
        '-show_entries','stream=codec_name','-of','json',str(request['video'])],timeout=10))
    codec=probe.get('streams',[{}])[0].get('codec_name')
    parser_name={'h264':'h264parse','hevc':'h265parse'}.get(codec)
    if parser_name is None:raise RuntimeError('Unsupported MPP QC codec: '+str(codec))
    expected_caps='video/x-h264' if codec=='h264' else 'video/x-h265'
    parser = Gst.ElementFactory.make(parser_name, 'parser')
    decoder = Gst.ElementFactory.make('mppvideodec', 'decoder')
    sink = Gst.ElementFactory.make('appsink', 'sink')
    if any(element is None for element in (source, demux, parser, decoder, sink)):
        raise RuntimeError('Required GStreamer MPP element unavailable')
    source.set_property('location', str(Path(request['video']).resolve()))
    decoder.set_property('format', 23)  # NV12 avoids unsupported RGA BGR conversion.
    decoder.set_property('ignore-error', False)
    sink.set_property('sync', False)
    sink.set_property('max-buffers', 2)
    sink.set_property('drop', False)
    sink.set_property('wait-on-eos', False)
    for element in (source, demux, parser, decoder, sink):
        pipeline.add(element)
    if not source.link(demux) or not parser.link(decoder) or not decoder.link(sink):
        raise RuntimeError('MPP pipeline link failed')

    def on_pad_added(_element, pad):
        caps = pad.get_current_caps()
        if caps and caps.get_structure(0).get_name() == expected_caps:
            target = parser.get_static_pad('sink')
            if not target.is_linked():
                pad.link(target)

    demux.connect('pad-added', on_pad_added)
    bus = pipeline.get_bus()
    i0, i1 = request.get('i0'), request.get('i1')
    rate = float(request.get('rate_hz', 2))
    black_thr = float(request.get('black_lum_thr', 8))
    if not np.isfinite(black_thr) or not 0 <= black_thr <= 255:
        raise ValueError('Invalid black-frame threshold')
    # Conservative lower bound for FFmpeg's BT.601 limited-range conversion
    # conversion: gray_mean >= 0.25 * expanded_Y_mean - 4. This bound includes
    # RGB saturation and integer rounding. Frames above it cannot be black.
    candidate_y = min(255, 16 + (black_thr + 4) * 4 * 219 / 255)
    lums, diffs, indices, all_lums, all_indices = [], [], [], [], []
    black_mask = []
    previous = None
    n_frames, fps, stride, first_pts, last_pts = 0, None, None, None, None
    started = time.monotonic()
    yuv = None
    color = None
    try:
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError('MPP pipeline failed to start')
        while True:
            sample = sink.emit('try-pull-sample', 5 * Gst.SECOND)
            msg = bus.pop_filtered(Gst.MessageType.ERROR)
            if msg is not None:
                error, _debug = msg.parse_error()
                raise RuntimeError(str(error))
            if sample is None:
                if sink.get_property('eos'):
                    break
                raise TimeoutError('MPP decoder stalled or unsupported codec')
            info = GstVideo.VideoInfo()
            if not info.from_caps(sample.get_caps()) or info.finfo.name != 'NV12':
                raise RuntimeError('Expected NV12 output')
            if info.width % 2 or info.height % 2:
                raise RuntimeError('Unsupported odd NV12 dimensions')
            if info.width % 16:
                raise RuntimeError('Unsupported unaligned width for legacy color conversion')
            current_fps = info.fps_n / info.fps_d if info.fps_d else 0
            if current_fps <= 0 or (fps is not None and abs(current_fps - fps) > 1e-6):
                raise RuntimeError('Invalid or changing frame rate')
            fps = current_fps
            stride_step = max(1, int(round(fps / rate)))
            buffer = sample.get_buffer()
            pts = int(buffer.pts)
            if pts == Gst.CLOCK_TIME_NONE or (last_pts is not None and pts <= last_pts):
                raise RuntimeError('Invalid or nonmonotonic decoded PTS')
            if first_pts is None:
                first_pts = pts
            last_pts = pts
            meta = GstVideo.buffer_get_video_meta(buffer)
            stride = meta.stride[0] if meta else info.stride[0]
            offset = meta.offset[0] if meta else info.offset[0]
            uv_stride = meta.stride[1] if meta else info.stride[1]
            uv_offset = meta.offset[1] if meta else info.offset[1]
            ok, mapped = buffer.map(Gst.MapFlags.READ)
            if not ok:
                raise RuntimeError('Cannot map MPP output')
            k = n_frames
            n_frames += 1
            try:
                # PyGObject materializes bytes on .data access: read it once.
                data = mapped.data
                if (stride < info.width or uv_stride < info.width
                        or offset + (info.height - 1) * stride + info.width > len(data)
                        or uv_offset + (info.height // 2 - 1) * uv_stride + info.width > len(data)):
                    raise RuntimeError('Invalid NV12 memory layout')
                if i0 is not None and not (i0 <= k <= i1):
                    continue
                # Respect both planes' actual stride and aligned-height offsets.
                shape = (info.height * 3 // 2, info.width)
                if yuv is None or yuv.shape != shape:
                    yuv = np.empty(shape, np.uint8)
                y = np.ndarray((info.height, info.width), np.uint8, buffer=data,
                               offset=offset, strides=(stride, 1))
                uv = np.ndarray((info.height // 2, info.width), np.uint8, buffer=data,
                                offset=uv_offset, strides=(uv_stride, 1))
                mean_y = float(y.mean())
                sampled = k % stride_step == 0
                if sampled or mean_y <= candidate_y:
                    yuv[:info.height] = y
                    yuv[info.height:] = uv
                    if color is None:
                        color = LegacyGray()
                    gray = color.convert(yuv)
                    luminance = float(gray.mean())
                    is_black = luminance < black_thr
                else:
                    # Used only as an internal brightness bound, not reported as
                    # an exact full-frame grayscale mean. Black mask is exact.
                    luminance = float(np.clip((mean_y - 16) * 255 / 219, 0, 255))
                    is_black = False
                all_lums.append(luminance)
                all_indices.append(k)
                black_mask.append(is_black)
                if sampled:
                    lums.append(luminance)
                    indices.append(k)
                    if previous is not None:
                        diffs.append(float(np.abs(gray.astype(np.float32) - previous).mean()))
                    previous = gray
            finally:
                buffer.unmap(mapped)
        if not n_frames:
            raise RuntimeError('MPP decoded no frames')
        return dict(n_frames=n_frames, fps=fps, lums=np.asarray(lums), diffs=np.asarray(diffs),
                    indices=np.asarray(indices, dtype=np.int64), all_lums=np.asarray(all_lums),
                    all_indices=np.asarray(all_indices, dtype=np.int64),
                    black_mask=np.asarray(black_mask, dtype=np.bool_), black_lum_thr=black_thr,
                    elapsed_seconds=time.monotonic() - started,
                    peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                    first_pts_ns=first_pts, last_pts_ns=last_pts, stride=stride,
                    backend='gstreamer-mpp-nv12')
    finally:
        pipeline.set_state(Gst.State.NULL)
        if color is not None:
            color.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = decode(json.load(sys.stdin))
        np.savez(args.output, **result)
    except Exception as error:
        print(type(error).__name__ + ': ' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
