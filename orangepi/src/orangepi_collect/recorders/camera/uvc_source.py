"""Bounded MJPEG UVC source shared by device discovery and recording.

The source never silently substitutes a different size/rate or device. Timestamp
metadata distinguishes host arrival from GStreamer PTS (not a hardware trigger).
"""
import json
from pathlib import Path
import time
from orangepi_collect.common import wrist_camera_settings


def validate_config(config):
    device = config['device']
    if not Path(device).is_absolute() or not device.startswith('/dev/'):
        raise ValueError('UVC device must be an absolute /dev/v4l/by-path path')
    if config.get('fourcc', 'MJPG') != 'MJPG':
        raise ValueError('This hardware decoder requires MJPG input')
    width, height, fps = (int(config[k]) for k in ('width', 'height', 'fps'))
    if min(width, height) <= 0 or fps != 30:
        raise ValueError('Use a supported positive image size and 30 fps (original wrist QC contract)')
    return device, width, height, fps


class UvcSource:
    def __init__(self, config, callback):
        self.camera_settings = None
        try:
            device, width, height, fps = validate_config(config)
            if not Path(device).exists():
                raise FileNotFoundError(device)
            self.camera_settings = wrist_camera_settings.configure_device(config)
            import gi
            gi.require_version('Gst', '1.0')
            from gi.repository import Gst
            self.Gst = Gst
            Gst.init(None)
            self.error = None
            self.expected_size=(width,height)
            self.count = 0
            self.callback = callback
            self.description = (
                'v4l2src device=' + json.dumps(device) + ' io-mode=mmap ! '
                'image/jpeg,width=%d,height=%d,framerate=%d/1 ! ' % (width, height, fps) +
                'appsink name=sink emit-signals=true sync=false max-buffers=2 drop=false')
            self.pipeline = Gst.parse_launch(self.description)
            self.bus = self.pipeline.get_bus()
            self.pipeline.get_by_name('sink').connect('new-sample', self._sample)
            if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                self.close()
                raise RuntimeError('UVC pipeline cannot start: ' + device)
        except Exception as exc:
            if self.camera_settings is not None and getattr(exc, 'report', None) is None:
                exc.report = self.camera_settings
            exc.report = wrist_camera_settings.error_report(config, exc)
            raise

    def _sample(self, sink):
        Gst = self.Gst
        sample = sink.emit('pull-sample')
        if sample is None:
            return Gst.FlowReturn.ERROR
        try:
            structure=sample.get_caps().get_structure(0)
            actual=(int(structure.get_value('width')),int(structure.get_value('height')))
            if actual!=self.expected_size:raise RuntimeError('UVC driver negotiated a different capture resolution: '+str(actual))
            buffer = sample.get_buffer()
            mono, wall = time.monotonic_ns(), time.time_ns()
            pts = int(buffer.pts)
            # Drop frames captured before START even if delivered after START.
            running = self.pipeline.get_clock().get_time() - self.pipeline.get_base_time()
            if pts == Gst.CLOCK_TIME_NONE or pts > running + Gst.SECOND:
                raise RuntimeError('UVC source has invalid buffer timestamp')
            capture_mono = mono - max(0, running - pts)
            payload = buffer.extract_dup(0, buffer.get_size())
            if not payload.startswith(b'\xff\xd8'):
                raise RuntimeError('UVC frame is not JPEG')
            self.count += 1
            self.callback(payload, pts, self.count - 1, mono, wall, capture_mono)
            return Gst.FlowReturn.OK
        except Exception as exc:
            self.error = str(exc)
            return Gst.FlowReturn.ERROR

    def health(self):
        if self.error:
            error = RuntimeError(self.error)
            error.report = self.camera_settings
            raise error
        msg = self.bus.pop_filtered(self.Gst.MessageType.ERROR)
        if msg:
            error, debug = msg.parse_error()
            exception = RuntimeError('UVC: %s %s' % (error, debug))
            exception.report = self.camera_settings
            raise exception

    def close(self):
        self.pipeline.set_state(self.Gst.State.NULL)


def probe(config, seconds=3, report=None):
    started=time.monotonic();source=None
    if report is not None:
        report.update(device=config.get('device'),requested_size=[config.get('width'),config.get('height')],
            requested_fps=config.get('fps'),window_seconds=seconds,required_frames=10)
    try:
        source = UvcSource(config, lambda *args: None)
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            source.health()
            time.sleep(.05)
        source.health()
        return source.count >= 10
    except Exception as error:
        if report is not None:report.update(error_type=type(error).__name__,error=str(error))
        raise
    finally:
        if report is not None:
            report.update(received_frames=source.count if source is not None else 0,elapsed_seconds=time.monotonic()-started)
        if source is not None:source.close()
