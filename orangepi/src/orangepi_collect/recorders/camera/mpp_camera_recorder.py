"""ROS/UVC camera recording with Rockchip MPP and the common session gate."""
import csv
import json
import os
import queue
import subprocess
import threading
import time
import gi
from orangepi_collect.recorders.camera.frame_queue import FrameQueue
gi.require_version('Gst', '1.0')
from gi.repository import Gst
from live_preview import publisher
from project_paths import devices
from orangepi_collect.common import wrist_camera_settings
from orangepi_collect.recorders.camera.uvc_source import UvcSource, validate_config
from orangepi_collect.recorders.camera.camd_source import (CamdH264Source, CamdError,
    snapshot_jpeg as camd_snapshot_jpeg)
# Viewer frames come from SNAPSHOT, which camd serves independently of the
# exclusive STREAM_H264 subscription this recorder holds. Measured on the device
# (RK3588, four cameras at 1920x1200): one frame costs ~33 ms wall but only
# ~18 ms CPU at scale=4, so the work is latency-bound and gets a thread per
# camera. Verified at this rate: recorded streams stayed at 30.00 Hz with zero
# gaps and zero camd frame drops.
#
# live_preview.Publisher.camera() additionally throttles each camera to one
# frame per 0.33 s, shared with the wrist preview path, so sampling faster than
# that only burns CPU. Sample slightly inside that window so a frame is always
# ready when the publisher accepts one.
PREVIEW_INTERVAL = 0.3
PREVIEW_SCALE = 4
PREVIEW_QUALITY = 70
from orangepi_collect.framework.gap_thresholds import (wrist_gap_level,
    headband_gap_level, EVENT_LIMIT, WRIST_WARN_NS, WRIST_ERROR_NS,
    HEADBAND_WARN_NS, HEADBAND_ERROR_NS)
from orangepi_collect.common.camera_resolution import jpeg_size,verify_resolution
CONFIG=devices()
WRIST_CAMERAS=CONFIG.get('wrist_cameras',{})

class CameraDataError(RuntimeError):
    """The camera stopped cleanly, but its recording failed data validation."""


class Camera:
    def __init__(self, owner, name, usb=False):
        try:
            self._initialize(owner, name, usb)
        except BaseException as exc:
            if usb and hasattr(self, 'path'):
                if self.camera_settings is not None and getattr(exc, 'report', None) is None:
                    exc.report = self.camera_settings
                self.camera_settings = wrist_camera_settings.error_report(self.config, exc)
                wrist_camera_settings.write_report(self.path / 'camera_controls.json', self.camera_settings)
            raise

    def _initialize(self, owner, name, usb):
        self.usb = usb
        self.owner, self.name = owner, name if usb else 'cam_ros_' + name
        self.config = WRIST_CAMERAS[name] if usb else CONFIG['cameras'][name]
        # Headband cameras now come from camd at its configured rate (30fps on
        # EgoRecord v0.1.10); the retired ROS bridge effectively delivered 20.
        self.fps = validate_config(self.config)[3] if usb else int(self.config.get('fps', 30))
        self.topic = None if usb else self.config.get('topic')
        self.capture_size=(int(self.config['width']),int(self.config['height']))
        self.observed_size=None
        self.input = None
        self.camera_settings = None
        self.path = owner.path / self.name
        self.path.mkdir()
        self.ready = False
        self.seen = 0
        self.last = time.monotonic()
        self.accepted = self.encoded = 0
        self.previous_stamp = None
        self.previous_source_pts = None
        if usb:
            self.frame_gap_qc = dict(schema_version=1, timestamp_field='source_pts_ns',
                clock_basis='V4L2_GStreamer_buffer_PTS_not_hardware_sync',
                warn_threshold_ns=WRIST_WARN_NS,error_threshold_ns=WRIST_ERROR_NS,
                comparison='strictly_greater_than',warn_count=0,error_count=0,
                max_interval_ns=0,events=[],events_truncated=False,worst_event=None)
        else:
            # camd reports no drop of its own when delivery skips frames, so the
            # recorded timestamps are the only evidence. Record the same shape
            # as the wrist path so offline QC can treat both alike.
            self.frame_gap_qc = dict(schema_version=1, timestamp_field='camd_ts_ns',
                clock_basis='camd_CLOCK_REALTIME_frame_grid',
                warn_threshold_ns=HEADBAND_WARN_NS,error_threshold_ns=HEADBAND_ERROR_NS,
                comparison='strictly_greater_than',warn_count=0,error_count=0,
                max_interval_ns=0,events=[],events_truncated=False,worst_event=None)
        self.first_stamp = None
        self._preview_stop = threading.Event()
        self._preview_thread = None
        self._h264_preview = None
        self._preview_frames = 0
        self._preview_errors = 0
        self._preview_close_error = None
        self.last_accepted = time.monotonic()
        # H.264 frames are already compressed. Absorb short storage stalls without
        # discarding frames; persistent stalls remain fatal and bounded.
        self.queue = queue.Queue(maxsize=24) if usb else FrameQueue(maxsize=120, max_bytes=16 * 1024 * 1024)
        self.max_timestamp_flush_seconds = 0.0
        self.file = (self.path / 'timestamps.csv').open('w', newline='')
        self.csv = csv.writer(self.file)
        # The stamp column is nanoseconds for both sources: camd reports
        # microseconds and the callback scales them, so name the unit honestly.
        self.csv.writerow(['frame_index','frames_unix_ns' if usb else 'camd_ts_ns','arrival_unix_ns','arrival_monotonic_ns','video_pts_ns','source_pts_ns' if usb else 'keyframe'])
        if usb:
            # Wrist cameras still deliver MJPEG, so decode and re-encode.
            pipeline = ('appsrc name=source is-live=true format=time block=false max-bytes=8388608 ' +
                        ('caps="image/jpeg,width=%d,height=%d,framerate=%d/1" ! jpegparse ! ' % (self.capture_size+(self.fps,))) +
                        'mppjpegdec format=NV12 ignore-error=false ! '
                        'mpph265enc name=encoder bps=8000000 gop=20 rc-mode=vbr ! h265parse ! '
                        'mp4mux ! filesink location="' + str(self.path / 'frames.mp4') + '"')
        else:
            # camd already hands us hardware-encoded H.264 Annex-B with SPS/PPS
            # on every IDR, so remux straight to MP4: no decode, no re-encode.
            pipeline = ('appsrc name=source is-live=true format=time block=false max-bytes=8388608 ' +
                        ('caps="video/x-h264,stream-format=byte-stream,alignment=au,'
                         'width=%d,height=%d,framerate=%d/1" ! ' % (self.capture_size+(self.fps,))) +
                        'h264parse name=encoder config-interval=-1 ! '
                        'mp4mux ! filesink location="' + str(self.path / 'frames.mp4') + '"')
        (self.path / 'pipeline.txt').write_text(pipeline)
        self.pipeline = Gst.parse_launch(pipeline)
        self.source = self.pipeline.get_by_name('source')
        self.bus = self.pipeline.get_bus()
        self.pipeline.get_by_name('encoder').get_static_pad('src').add_probe(Gst.PadProbeType.BUFFER, self.count_encoded)
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.pipeline.set_state(Gst.State.NULL)
            raise RuntimeError(self.name + ': pipeline cannot start')
        self.thread = threading.Thread(target=self.feed, daemon=True)
        self.thread.start()
        try:
            if usb:
                self.input = UvcSource(self.config, self.usb_callback, strict_frame=self._strict_usb_frame)
                self.camera_settings = self.input.camera_settings
                wrist_camera_settings.write_report(self.path / 'camera_controls.json', self.camera_settings)
                (self.path/'source_pipeline.txt').write_text(self.input.description)
            else:
                self.input = CamdH264Source(self.name, self.camd_callback)
                (self.path/'source_pipeline.txt').write_text(self.input.description, encoding='utf-8')
                self._preview_thread = threading.Thread(
                    target=self._preview_loop, name='preview-' + self.name, daemon=True)
                self._preview_thread.start()
        except BaseException:
            self._preview_stop.set()
            if self._preview_thread is not None:
                self._preview_thread.join(3)
            if self.input is not None:
                self.input.close()
            self.queue.put(None)
            self.thread.join(3)
            self.pipeline.set_state(Gst.State.NULL)
            self.file.close()
            raise

    def _strict_usb_frame(self,capture_mono):
        with self.owner.gate:
            return self.owner.active and capture_mono>=self.owner.start_ns

    def usb_callback(self, payload, pts, sequence, mono, wall, capture_mono):
        self._verify_source(payload)
        publisher().camera(self.name, payload)
        self.seen += 1
        self.ready = True
        self.last = time.monotonic()
        with self.owner.gate:
            if not self.owner.active or capture_mono < self.owner.start_ns:
                return
            try:
                self.queue.put_nowait((payload, wall, pts, mono, wall))
            except queue.Full:
                self.owner.fail(self.name + ': camera queue overflow')

    def count_encoded(self, pad, info):
        self.encoded += 1
        return Gst.PadProbeReturn.OK

    def _preview_loop(self):
        """Reuse this owner's H.264; preview never opens a second subscription."""
        from orangepi_collect.recorders.camera.h264_preview import H264JpegPreview
        retry_at=0.;snapshot_at=0.
        def publish_jpeg(payload):
            publisher().camera(self.name,payload);self._preview_frames+=1
        try:
            while not self._preview_stop.wait(.1):
                handle=publisher();now=time.monotonic()
                if not handle.wants_camera(self.name):
                    preview,self._h264_preview=self._h264_preview,None
                    if preview is not None:self._close_preview(preview)
                    continue
                try:
                    size=(1920,1200) if handle.wants_full_camera(self.name) else (640,400)
                    if self._h264_preview is not None and self._h264_preview.size!=size:
                        preview,self._h264_preview=self._h264_preview,None;self._close_preview(preview)
                    if self._h264_preview is not None:self._h264_preview.health()
                    elif now>=retry_at:self._h264_preview=H264JpegPreview(publish_jpeg,*size)
                except Exception:
                    self._preview_errors+=1;retry_at=now+10
                    preview,self._h264_preview=self._h264_preview,None
                    if preview is not None:
                        try:self._close_preview(preview)
                        except Exception:pass
                if self._h264_preview is None and now>=snapshot_at:
                    snapshot_at=now+PREVIEW_INTERVAL
                    try:
                        _stamp,jpeg=camd_snapshot_jpeg(self.name,scale=PREVIEW_SCALE,quality=PREVIEW_QUALITY)
                        publish_jpeg(jpeg)
                    except Exception:self._preview_errors+=1
        finally:
            preview,self._h264_preview=self._h264_preview,None
            if preview is not None:
                try:self._close_preview(preview)
                except Exception:pass

    def _close_preview(self, preview):
        try:
            preview.close()
            if preview.thread.is_alive():
                raise RuntimeError('preview feeder thread did not stop')
            result,current,pending=preview.pipeline.get_state(5 * preview.Gst.SECOND)
            if (result != preview.Gst.StateChangeReturn.SUCCESS
                    or current != preview.Gst.State.NULL
                    or pending != preview.Gst.State.VOID_PENDING):
                raise RuntimeError('preview pipeline did not reach NULL state')
        except Exception as exc:
            self._preview_close_error = exc
            raise

    def camd_callback(self, payload, ts_us, keyframe, mono, wall):
        """One H.264 access unit from camd. ts_us is CLOCK_REALTIME microseconds."""
        preview=self._h264_preview
        if preview is not None:preview.offer(payload,ts_us,keyframe)
        if self.observed_size is None:
            self.observed_size = self.capture_size
            (self.path/'capture_resolution.json').write_text(json.dumps(dict(
                width=self.capture_size[0], height=self.capture_size[1], fps=self.fps,
                preview_settings_affect_recording=False,
                source='camd STREAM_H264'), indent=2), encoding='utf-8')
        self.seen += 1
        stamp = int(ts_us) * 1000
        # `ready` means "this device is producing data" and gates START, so set
        # it on the first frame. Starting the MP4 on an IDR is enforced
        # separately below, inside the capture gate.
        self.ready = True
        self.last = time.monotonic()
        with self.owner.gate:
            if not self.owner.active or mono < self.owner.start_ns:
                return
            # Reject frames exposed before the common start boundary.
            if stamp < self.owner.start_wall_ns:
                return
            if not self.accepted and not keyframe:
                return
            try:
                self.queue.put_nowait((payload, stamp, 1 if keyframe else 0, mono, wall))
            except queue.Full:
                self.owner.fail(self.name + ': camera queue overflow ' + json.dumps(self.queue.stats(), sort_keys=True))

    def _verify_source(self,payload):
        actual=verify_resolution(jpeg_size(payload),self.capture_size,self.name)
        if self.observed_size is None:
            self.observed_size=actual
            (self.path/'capture_resolution.json').write_text(json.dumps(dict(width=actual[0],height=actual[1],fps=self.fps,preview_settings_affect_recording=False,source='UVC MJPEG' if self.usb else self.topic),indent=2),encoding='utf-8')

    def _note_wrist_gap(self, source_pts, wall):
        if self.previous_source_pts is None:
            return
        interval = int(source_pts) - int(self.previous_source_pts)
        if interval <= 0:
            raise RuntimeError('wrist source PTS did not increase')
        level = wrist_gap_level(interval)
        stats = self.frame_gap_qc
        if interval > stats['max_interval_ns']:
            stats['max_interval_ns'] = interval
        if level is None:
            return
        stats['error_count' if level == 'ERROR' else 'warn_count'] += 1
        event = dict(before_index=self.accepted-1,after_index=self.accepted,
            before_source_pts_ns=int(self.previous_source_pts),after_source_pts_ns=int(source_pts),
            after_unix_ns=int(wall),interval_ns=interval,level=level)
        if stats['worst_event'] is None or interval > stats['worst_event']['interval_ns']:
            stats['worst_event'] = event
        if len(stats['events']) < EVENT_LIMIT:
            stats['events'].append(event)
        else:
            stats['events_truncated'] = True
        # A quality ERROR must not close the capture gate. Offline QC records
        # the same ERROR and preserves all original samples for review/retry.
        self.owner.warn(self.name + ': QC_' + level +
            ' source_pts_ns adjacent frame interval=' + str(interval) +
            ' ns; recording continues')

    def _note_headband_gap(self, stamp, wall):
        """Record a delivery gap in the camd stream as QC evidence.

        camd leaves frames_dropped at zero when its delivery skips frames, so
        the recorded timestamps are the only place the loss is visible. Missing
        frames are counted from the 30 fps grid.
        """
        if self.previous_stamp is None:
            return
        interval = int(stamp) - int(self.previous_stamp)
        level = headband_gap_level(interval)
        stats = self.frame_gap_qc
        if interval > stats['max_interval_ns']:
            stats['max_interval_ns'] = interval
        if level is None:
            return
        stats['error_count' if level == 'ERROR' else 'warn_count'] += 1
        frame_ns = int(1e9 / self.fps)
        event = dict(before_index=self.accepted-1, after_index=self.accepted,
            before_ts_ns=int(self.previous_stamp), after_ts_ns=int(stamp),
            after_unix_ns=int(wall), interval_ns=interval, level=level,
            estimated_missing_frames=max(0, int(round(interval / frame_ns)) - 1))
        if stats['worst_event'] is None or interval > stats['worst_event']['interval_ns']:
            stats['worst_event'] = event
        if len(stats['events']) < EVENT_LIMIT:
            stats['events'].append(event)
        else:
            stats['events_truncated'] = True
        # A quality finding must not close the capture gate; offline QC reports
        # the same evidence and every captured frame is preserved.
        self.owner.warn(self.name + ': QC_' + level +
            ' camd delivery gap=' + str(interval) + ' ns (~' +
            str(event['estimated_missing_frames']) + ' frame(s)); recording continues')

    def feed(self):
        try:
            while True:
                item = self.queue.get()
                if item is None:
                    break
                payload, stamp, sequence, mono, wall = item
                if self.previous_stamp is not None and stamp <= self.previous_stamp:
                    raise RuntimeError('frame timestamp did not increase: previous=%d current=%d %s=%d arrival=%d'
                                       % (self.previous_stamp, stamp,
                                          'source_pts' if self.usb else 'keyframe',
                                          sequence, wall))
                if self.first_stamp is None:
                    self.first_stamp = stamp
                    self.first_mono = mono
                if self.usb:
                    self._note_wrist_gap(sequence,wall)
                else:
                    self._note_headband_gap(stamp,wall)
                pts = (mono - self.first_mono) if self.usb else (stamp - self.first_stamp)
                if self.source.get_property('current-level-bytes') > 8388608:
                    raise RuntimeError('hardware decoder input stalled' if self.usb
                                       else 'mp4 muxer input stalled')
                buf = Gst.Buffer.new_allocate(None, len(payload), None)
                buf.fill(0,payload)
                buf.pts = pts
                buf.dts = pts
                buf.duration = int(1e9/self.fps)
                if self.source.emit('push-buffer',buf) != Gst.FlowReturn.OK:
                    raise RuntimeError('hardware pipeline rejected frame')
                self.csv.writerow([self.accepted,stamp,wall,mono,pts,sequence])
                self.accepted += 1
                self.last_accepted = time.monotonic()
                self.previous_stamp = stamp
                if self.usb:
                    self.previous_source_pts = int(sequence)
                if self.accepted % 20 == 0:
                    flush_started = time.monotonic()
                    self.file.flush()
                    self.max_timestamp_flush_seconds = max(
                        self.max_timestamp_flush_seconds, time.monotonic() - flush_started)
        except Exception as exc:
            self.owner.fail(self.name + ': ' + str(exc))

    def health(self):
        if self.input is not None:
            try:
                self.input.health()
            except Exception as exc:
                error = RuntimeError(self.name + ': ' + str(exc))
                if self.usb:
                    error.report = wrist_camera_settings.error_report(self.config, exc)
                raise error from exc
        msg = self.bus.pop_filtered(Gst.MessageType.ERROR)
        if msg:
            error, debug = msg.parse_error()
            raise RuntimeError(self.name + ': ' + str(error) + ' ' + str(debug))
        if self.owner.started and time.monotonic_ns()-self.owner.start_ns > 3000000000 and time.monotonic()-self.last_accepted>3:
            raise RuntimeError(self.name + ': no data accepted for 3 seconds')
        if self.accepted - self.encoded > 60:
            raise RuntimeError(self.name + ': encoder stalled')

    def close(self):
        resource_errors=[]
        data_errors=[]
        self._preview_stop.set()
        if self._preview_thread is not None:
            self._preview_thread.join(3)
            if self._preview_thread.is_alive():
                resource_errors.append('preview thread did not stop')
        if self._preview_close_error is not None:
            resource_errors.append('preview close failed: '+str(self._preview_close_error))
        if self.input is not None:
            try:
                self.input.close()
            except Exception as exc:
                resource_errors.append('input source did not stop: '+str(exc))
            else:
                if self.usb and getattr(self.input,'last_invalid_frame',None):
                    try:
                        (self.path/'uvc_input_diagnostics.json').write_text(json.dumps(dict(
                            invalid_preparation_frames=self.input.invalid_preparation_frames,
                            last_invalid_frame=self.input.last_invalid_frame),indent=2),encoding='utf-8')
                    except Exception as exc:
                        data_errors.append('could not write UVC diagnostics: '+str(exc))
        if self.thread.is_alive():
            try:
                self.queue.put(None, timeout=3)
            except Exception as exc:
                resource_errors.append('could not stop camera feeder: '+str(exc))
            self.thread.join(8)
            if self.thread.is_alive():
                resource_errors.append('camera feeder did not stop')
        if not self.thread.is_alive():
            try:
                result=self.source.emit('end-of-stream')
                if result != Gst.FlowReturn.OK:
                    data_errors.append('pipeline rejected end-of-stream')
                if self.accepted:
                    msg = self.bus.timed_pop_filtered(15 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
                    if msg is None:
                        data_errors.append('hardware encoder did not finish MP4 (EOS timeout)')
                    elif msg.type != Gst.MessageType.EOS:
                        error, debug = msg.parse_error()
                        data_errors.append('hardware encoder did not finish MP4: %s %s' % (error,debug))
            except Exception as exc:
                data_errors.append('hardware pipeline finalization failed: '+str(exc))
        try:
            transition=self.pipeline.set_state(Gst.State.NULL)
            if transition == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError('recording pipeline refused NULL state')
            result,current,pending=self.pipeline.get_state(5 * Gst.SECOND)
            if (result != Gst.StateChangeReturn.SUCCESS or current != Gst.State.NULL
                    or pending != Gst.State.VOID_PENDING):
                raise RuntimeError('recording pipeline did not reach NULL state')
        except Exception as exc:
            resource_errors.append(str(exc))
        try:
            self.file.flush()
        except Exception as exc:
            resource_errors.append('timestamp file flush failed: '+str(exc))
        try:
            if not self.file.closed:
                os.fsync(self.file.fileno())
        except Exception as exc:
            resource_errors.append('timestamp file sync failed: '+str(exc))
        try:
            self.file.close()
        except Exception as exc:
            resource_errors.append('timestamp file close failed: '+str(exc))
        if resource_errors:
            raise RuntimeError('camera resource shutdown unconfirmed: '+'; '.join(resource_errors))
        if not self.accepted:
            data_errors.append('no recorded video frames')
        stream=None
        if self.accepted and not data_errors:
            try:
                result = subprocess.run(['ffprobe','-v','error','-select_streams','v:0','-count_packets',
                    '-show_entries','stream=codec_name,width,height,nb_read_packets,duration',
                    '-of','json',str(self.path/'frames.mp4')], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
                text = result.stdout.decode(errors='replace')
                if result.returncode or result.stderr.strip():
                    raise ValueError('ffprobe failed: '+ result.stderr.decode(errors='replace'))
                data = json.loads(text[text.index('{'):])
                stream = data['streams'][0]
                verify_resolution((stream['width'],stream['height']),self.capture_size,self.name+' saved video')
                expected_codec = 'hevc' if self.usb else 'h264'
                if stream['codec_name'] != expected_codec or int(stream['nb_read_packets']) != self.accepted:
                    raise ValueError('encoded packet count differs from accepted frame count')
                (self.path/'video_check.json').write_text(json.dumps(data,indent=2))
            except Exception as exc:
                data_errors.append(str(exc))
        try:
            # Both camera kinds now carry delivery-gap evidence.
            (self.path/'frame_gap_qc.json').write_text(json.dumps(self.frame_gap_qc,ensure_ascii=False,indent=2),encoding='utf-8')
        except Exception as exc:
            data_errors.append('could not write frame-gap evidence: '+str(exc))
        if data_errors:
            raise CameraDataError('; '.join(data_errors))
        return dict(frames=self.accepted, encoded=self.encoded, probe=stream,
                    writer_queue=self.queue.stats() if not self.usb else None,
                    max_timestamp_flush_seconds=self.max_timestamp_flush_seconds,
                    decoder='mppjpegdec' if self.usb else None,
                    encoder='mpph265enc' if self.usb else 'camd (hardware H.264)',
                    topic=self.topic,
                    source='camd STREAM_H264' if not self.usb else 'UVC MJPEG',
                    device=self.config.get('device'), fps=self.fps,
                    camera_controls=self.camera_settings,
                    preview_frames=None if self.usb else self._preview_frames,
                    preview_errors=None if self.usb else self._preview_errors,
                    frame_gap_qc=self.frame_gap_qc,
                    timestamp_basis='host frame arrival; source PTS retained; no hardware synchronization' if self.usb
                        else 'camd CLOCK_REALTIME frame grid (same domain as /dev/icm20602)')

