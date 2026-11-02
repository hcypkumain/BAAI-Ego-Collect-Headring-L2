"""Bounded, exact-ordinal playback of the recorder's CFR MP4 files.

Only existing media is read. A subprocess owns GStreamer/MPP so a stalled native
decoder cannot block the HTTP service's cleanup. No videorate, leaky queue or
appsink drop is used: pipe backpressure slows playback instead of losing frames.
The HTTP caller controls pacing and must request the next ordinal after display.
"""
from collections import deque
import csv
import json
import math
import os
from pathlib import Path
import queue
import signal
import struct
import subprocess
import sys
import threading
import time

MAGIC = b'RPF1'
HEADER = struct.Struct('>4sIQq')  # magic, JPEG bytes, exact ordinal, encoded PTS
MAX_JPEG = 2 * 1024 * 1024
MAX_INDEX_ROWS = 2_000_000
MAX_INDEX_BYTES = 256 * 1024 * 1024
MAX_CSV_LINE = 16 * 1024


class ReplayVideoError(Exception):
    def __init__(self, message, status=422):
        super().__init__(message)
        self.status = status


def _bounded_csv_lines(handle,deadline,cancel_event=None):
    while True:
        if time.monotonic()>deadline:raise ReplayVideoError('逐帧索引读取超时',503)
        if cancel_event is not None and cancel_event.is_set():raise ReplayVideoError('回看操作已取消',503)
        line=handle.readline(MAX_CSV_LINE+1)
        if not line:return
        if len(line)>MAX_CSV_LINE:raise ReplayVideoError('逐帧索引包含异常长行',422)
        yield line


def _rate(text):
    try:
        a, b = str(text).split('/')
        value = float(a) / float(b)
        return value if math.isfinite(value) and 0 < value <= 240 else None
    except (ValueError, TypeError, ZeroDivisionError):
        return None


def _identity(path):
    try:
        if path.is_symlink() or not path.is_file():
            raise ReplayVideoError('回看文件不是普通录制文件', 404)
        s = path.stat()
    except OSError as exc:
        raise ReplayVideoError('录制文件无法读取: ' + str(exc), 404)
    if not s.st_size:
        raise ReplayVideoError('这一路没有落盘视频帧', 404)
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)


def _run_probe(command,timeout,cancel_event=None):
    if cancel_event is None:
        return subprocess.run(command,capture_output=True,timeout=timeout)
    if cancel_event.is_set():raise ReplayVideoError('回看操作已取消',503)
    process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    deadline=time.monotonic()+timeout
    try:
        while True:
            if cancel_event.is_set():raise ReplayVideoError('回看操作已取消',503)
            remaining=deadline-time.monotonic()
            if remaining<=0:raise subprocess.TimeoutExpired(command,timeout)
            try:
                stdout,stderr=process.communicate(timeout=min(.1,remaining))
                return subprocess.CompletedProcess(command,process.returncode,stdout,stderr)
            except subprocess.TimeoutExpired:
                continue
    finally:
        if process.poll() is None:
            process.kill()
            try:process.communicate(timeout=.7)
            except subprocess.TimeoutExpired:pass
        for handle in (process.stdout,process.stderr):
            try:handle.close()
            except OSError:pass


def probe_video(path, timeout=6, cancel_event=None):
    """Inspect actual MP4 metadata and its existing frame index without decoding."""
    path = Path(path)
    deadline=time.monotonic()+timeout
    identity = _identity(path)
    command = ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
               '-show_entries', 'stream=codec_name,width,height,nb_frames,avg_frame_rate,r_frame_rate,time_base,start_time',
               '-of', 'json', str(path)]
    try:
        p = _run_probe(command,timeout,cancel_event)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReplayVideoError('视频索引工具不可用: ' + str(exc), 503)
    if p.returncode:
        raise ReplayVideoError('视频容器无法读取: ' + p.stderr.decode('utf-8', 'replace')[-500:])
    # The board's FFmpeg prints an RGA version line before JSON.
    raw = p.stdout.decode('utf-8', 'replace')
    begin, end = raw.find('{'), raw.rfind('}')
    try:
        data = json.loads(raw[begin:end+1])
        streams = data['streams']
        if len(streams) != 1:
            raise ValueError('expected one selected video stream')
        stream = streams[0]
        codec = stream['codec_name']
        width, height = int(stream['width']), int(stream['height'])
        count = int(stream['nb_frames'])
        fps = _rate(stream.get('avg_frame_rate'))
        nominal = _rate(stream.get('r_frame_rate'))
        n, d = stream['time_base'].split('/')
        tick_ns = 1e9 * int(n) / int(d)
        start_ns = round(float(stream.get('start_time', '0')) * 1e9)
        if (codec not in ('hevc', 'h264') or not 0 < width <= 4096 or
                not 0 < height <= 4096 or width * height > 12_000_000 or
                not 0 < count <= MAX_INDEX_ROWS or fps is None or nominal is None or
                abs(fps-nominal) > 1e-6 or not 0 < tick_ns <= 5_000_000):
            raise ValueError('unsupported dimensions/frame index/CFR timebase')
    except (KeyError, ValueError, TypeError, ZeroDivisionError) as exc:
        raise ReplayVideoError('视频不是已验证的完整恒帧率录制: ' + str(exc))
    index = path.parent / 'timestamps.csv'
    try:
        if index.is_symlink() or not index.is_file() or index.stat().st_size > MAX_INDEX_BYTES:
            raise ValueError('missing or oversized timestamps.csv')
        rows = 0
        with index.open(newline='', encoding='utf-8-sig') as handle:
            reader = csv.DictReader(_bounded_csv_lines(handle,deadline,cancel_event))
            if 'frame_index' not in (reader.fieldnames or ()):
                raise ValueError('missing frame_index column')
            for row in reader:
                if rows%1024==0 and cancel_event is not None and cancel_event.is_set():
                    raise ReplayVideoError('回看操作已取消',503)
                if rows >= MAX_INDEX_ROWS or int(row['frame_index']) != rows:
                    raise ValueError('frame index is not contiguous from zero')
                rows += 1
        if rows != count:
            raise ValueError('MP4 frame count and timestamp index differ: %d/%d' % (count, rows))
    except (OSError, ValueError, TypeError, csv.Error) as exc:
        raise ReplayVideoError('逐帧索引无法核验: ' + str(exc))
    if _identity(path) != identity:
        raise ReplayVideoError('录制文件在建立索引时发生变化', 409)
    return dict(path=str(path), identity=identity, codec=codec, width=width,
                height=height, frames=count, fps=fps, start_ns=start_ns,
                tick_ns=tick_ns, pts_tolerance_ns=max(1000, round(2*tick_ns)),
                timestamp_basis='encoded CFR MP4 PTS; capture CSV timestamps remain separate')


class VideoDecoder:
    """One bounded selected-file decoder; repeated GET of a frame is idempotent."""
    def __init__(self, max_width=480, max_height=300, quality=88,
                 idle_seconds=15, frame_timeout=6, _worker_command=None):
        if not (32 <= max_width <= 800 and 32 <= max_height <= 480 and 40 <= quality <= 95):
            raise ValueError('invalid replay image bounds')
        if not (.1 <= frame_timeout <= 30 and 1 <= idle_seconds <= 120):
            raise ValueError('invalid replay time bounds')
        self.max_width, self.max_height, self.quality = int(max_width), int(max_height), int(quality)
        self.idle_seconds, self.frame_timeout = float(idle_seconds), float(frame_timeout)
        self._worker_command = _worker_command
        self._lock = threading.RLock()
        self._quit = threading.Event()
        self._cancel=threading.Event();self._inflight=threading.Event()
        self._process = None
        self._stream_stop = None
        self._reader = self._stderr_reader = None
        self._frames = None
        self._errors = deque(maxlen=12)
        self._meta = None
        self._last_index = None
        self._last_jpeg = None
        self._last_pts = None
        self._last_used = time.monotonic()
        self._closed = False
        self._stats = dict(worker_starts=0, frames_returned=0, duplicate_requests=0,
                           decoded_frames=0, seek_restarts=0, linear_fallbacks=0,
                           errors=0, idle_releases=0)
        self._janitor = threading.Thread(target=self._idle, daemon=True)
        self._janitor.start()

    def _idle(self):
        while not self._quit.wait(min(1., self.idle_seconds/2)):
            with self._lock:
                if self._process is not None and time.monotonic()-self._last_used >= self.idle_seconds:
                    self._stats['idle_releases'] += 1
                    self._stop_worker()

    def _stderr(self, process, stop):
        # Read chunks rather than unbounded lines from noisy BSP native libraries.
        while not stop.is_set():
            part = process.stderr.read(512)
            if not part:
                return
            self._errors.append(part.decode('utf-8', 'replace'))

    @staticmethod
    def _read_exact(handle, size):
        pieces = []
        remaining = size
        while remaining:
            part = handle.read(remaining)
            if not part:
                if remaining == size:
                    return None
                raise ReplayVideoError('视频解码输出在一帧中途结束')
            pieces.append(part)
            remaining -= len(part)
        return b''.join(pieces)

    def _read_frames(self, process, destination, stop):
        try:
            while not stop.is_set():
                header = self._read_exact(process.stdout, HEADER.size)
                if header is None:
                    item = ReplayVideoError('视频在请求帧之前结束', 422)
                    break
                magic, length, index, pts = HEADER.unpack(header)
                if magic != MAGIC or not 4 <= length <= MAX_JPEG:
                    raise ReplayVideoError('视频解码输出格式或大小异常')
                jpeg = self._read_exact(process.stdout, length)
                if jpeg is None or not jpeg.startswith(b'\xff\xd8') or not jpeg.endswith(b'\xff\xd9'):
                    raise ReplayVideoError('视频解码输出不是完整JPEG帧')
                item = (index, pts, jpeg)
                while not stop.is_set():
                    try:
                        destination.put(item, timeout=.1)
                        break
                    except queue.Full:
                        pass
            else:
                return
        except Exception as exc:
            item = exc if isinstance(exc, ReplayVideoError) else ReplayVideoError(str(exc))
        while not stop.is_set():
            try:
                destination.put(item, timeout=.1)
                return
            except queue.Full:
                pass

    def _start_worker(self, start_index):
        self._stop_worker()
        if self._quit.is_set():raise ReplayVideoError('回看操作已取消',503)
        meta = self._meta
        width, height = meta['width'], meta['height']
        scale = min(1., self.max_width/width, self.max_height/height)
        width, height = max(2, int(width*scale)//2*2), max(2, int(height*scale)//2*2)
        period = 1e9/meta['fps']
        target = meta['start_ns'] + round(start_index*period)
        # Key-unit seeking retains original frame timestamps. Accurate seeking
        # inside a frame can clip its PTS to segment.start, falsely making the
        # preceding image look like the requested ordinal.
        seek_ns = max(0, target) if start_index else 0
        if self._worker_command:
            command = self._worker_command(meta, start_index, seek_ns)
        else:
            command = [sys.executable, '-B', str(Path(__file__).resolve()), '--worker',
                       '--path', meta['path'], '--codec', meta['codec'],
                       '--width', str(width), '--height', str(height), '--quality', str(self.quality),
                       '--start-index', str(start_index), '--seek-ns', str(seek_ns),
                       '--fps', str(meta['fps']), '--pts-offset-ns', str(meta['start_ns']),
                       '--pts-tolerance-ns', str(meta['pts_tolerance_ns'])]
        kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                      bufsize=0)
        if os.name == 'posix':
            kwargs['start_new_session'] = True
        try:
            self._process = subprocess.Popen(command, **kwargs)
        except OSError as exc:
            self._process = None
            raise ReplayVideoError('无法启动硬件回看解码: ' + str(exc), 503)
        self._stats['worker_starts'] += 1
        self._frames = queue.Queue(maxsize=2)
        self._stream_stop = threading.Event()
        self._errors.clear()
        self._reader = threading.Thread(target=self._read_frames,
            args=(self._process,self._frames,self._stream_stop), daemon=True)
        self._stderr_reader = threading.Thread(target=self._stderr,
            args=(self._process,self._stream_stop), daemon=True)
        self._reader.start();self._stderr_reader.start()
        self._next_index = start_index

    def _stop_worker(self):
        process = self._process
        if process is None:
            return
        self._process = None
        if self._stream_stop:
            self._stream_stop.set()
        if process.poll() is None:
            try:
                if os.name == 'posix':os.killpg(process.pid, signal.SIGTERM)
                else:process.terminate()
                process.wait(timeout=.7)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    if os.name == 'posix':os.killpg(process.pid, signal.SIGKILL)
                    else:process.kill()
                    process.wait(timeout=.7)
                except (OSError, subprocess.TimeoutExpired):pass
        for reader in (self._reader,self._stderr_reader):
            if reader and reader is not threading.current_thread():reader.join(timeout=.3)
        for handle in (process.stdout,process.stderr):
            try:handle.close()
            except OSError:pass
        self._reader = self._stderr_reader = None
        self._frames = self._stream_stop = None

    def _next(self, expected, deadline):
        while True:
            if self._quit.is_set() or self._cancel.is_set():raise ReplayVideoError('回看操作已取消',503)
            remaining=deadline-time.monotonic()
            if remaining<=0:
                raise ReplayVideoError('完整逐帧解码超时；未跳过请求帧',503)
            try:
                item=self._frames.get(timeout=min(.1,remaining))
                break
            except queue.Empty:
                continue
        if isinstance(item,Exception):
            diagnostic=''.join(list(self._errors))[-500:].strip()
            raise ReplayVideoError(str(item)+(' / '+diagnostic if diagnostic else ''),getattr(item,'status',422))
        ordinal,pts,jpeg=item
        if ordinal != expected:
            raise ReplayVideoError('解码帧序号不连续；已停止完整回看',409)
        target=self._meta['start_ns']+round(expected*1e9/self._meta['fps'])
        if pts < 0 or abs(pts-target)>self._meta['pts_tolerance_ns']:
            raise ReplayVideoError('解码时间戳与请求帧不符；未用邻近帧替代',409)
        self._stats['decoded_frames'] += 1
        self._next_index=expected+1
        return pts,jpeg

    def get_frame(self,path,index,fps=None):
        if isinstance(index,bool) or not isinstance(index,int) or not 0 <= index < MAX_INDEX_ROWS:
            raise ReplayVideoError('回看帧序号无效',400)
        if fps is not None and (isinstance(fps,bool) or not isinstance(fps,(int,float)) or
                                not math.isfinite(fps) or not 0 < fps <= 240):
            raise ReplayVideoError('回看帧率无效',400)
        path=Path(path).absolute()
        with self._lock:
            if self._closed:raise ReplayVideoError('回看解码服务已关闭',503)
            self._cancel.clear();self._inflight.set()
            self._last_used=time.monotonic()
            try:
                identity=_identity(path)
                if self._meta is not None and str(path)==self._meta['path'] and identity!=self._meta['identity']:
                    self._stop_worker();self._meta=None;self._last_jpeg=None
                    raise ReplayVideoError('源录制已发生变化，请重新选择采集段',409)
                if self._meta is None or str(path)!=self._meta['path']:
                    self._stop_worker()
                    self._meta=probe_video(path,timeout=self.frame_timeout,cancel_event=self._cancel)
                    self._last_index=self._last_jpeg=None
                if index>=self._meta['frames']:
                    raise ReplayVideoError('该路已到最后一帧',404)
                if self._last_index==index and self._last_jpeg is not None:
                    self._stats['duplicate_requests']+=1
                    return self._last_jpeg
                deadline=time.monotonic()+self.frame_timeout
                needs_start=self._process is None or index!=getattr(self,'_next_index',None)
                if needs_start:
                    self._stats['seek_restarts']+=int(index>0)
                    self._start_worker(index)
                try:pts,jpeg=self._next(index,deadline)
                except ReplayVideoError as exc:
                    # MPP/demux key-unit seek support is verified by actual PTS.
                    # If unsupported, restart from zero and walk the exact index;
                    # this is bounded by the same request deadline, never a jump.
                    if needs_start and index>0 and exc.status==409:
                        self._stats['linear_fallbacks']+=1
                        self._start_worker(0)
                        for n in range(index+1):pts,jpeg=self._next(n,deadline)
                    else:raise
                self._last_index,self._last_pts,self._last_jpeg=index,pts,jpeg
                self._stats['frames_returned']+=1
                return jpeg
            except ReplayVideoError:
                self._stats['errors']+=1
                self._stop_worker()
                raise
            finally:self._last_used=time.monotonic();self._inflight.clear()

    def release(self):
        """Release native resources while retaining a reusable service object."""
        self._cancel.set()
        with self._lock:self._stop_worker()

    def is_active(self):
        process=self._process
        return self._inflight.is_set() or process is not None and process.poll() is None

    def close(self):
        self._closed=True
        self._cancel.set()
        self._quit.set()
        with self._lock:
            self._stop_worker()
        if self._janitor is not threading.current_thread():self._janitor.join(timeout=1)

    def health(self):
        with self._lock:
            return dict(self._stats,active=self._process is not None,closed=self._closed,
                last_index=self._last_index,last_pts_ns=self._last_pts,
                source_frames=self._meta['frames'] if self._meta else None,
                source_fps=self._meta['fps'] if self._meta else None,
                max_width=self.max_width,max_height=self.max_height,
                jpeg_quality=self.quality,display_derivation='aspect-preserving scaled JPEG; original recording unchanged',
                complete_frame_priority=True,queue_capacity=2)


def _worker(args):
    """Native decoding subprocess. stdout is a dedicated bounded frame protocol."""
    output_fd=os.dup(1)
    os.dup2(2,1)  # BSP native stdout noise must not corrupt the frame protocol.
    output=os.fdopen(output_fd,'wb',buffering=0)
    import gi
    gi.require_version('Gst','1.0')
    from gi.repository import Gst
    Gst.init(None)
    pipeline=Gst.Pipeline.new('recording-replay')
    names=['filesrc','qtdemux','h265parse' if args.codec=='hevc' else 'h264parse',
           'mppvideodec','jpegenc','appsink']
    elements=[Gst.ElementFactory.make(name,None) for name in names]
    if any(x is None for x in elements):
        raise RuntimeError('Required MPP/JPEG pipeline element is unavailable')
    source,demux,parser,decoder,encoder,sink=elements
    source.set_property('location',args.path)
    decoder.set_property('ignore-error',False)
    decoder.set_property('dma-feature',False)
    decoder.set_property('width',args.width)
    decoder.set_property('height',args.height)
    decoder.set_property('format',2)  # I420, directly accepted by jpegenc.
    encoder.set_property('quality',args.quality)
    sink.set_property('sync',False)
    sink.set_property('drop',False)
    sink.set_property('max-buffers',2)
    sink.set_property('emit-signals',False)
    sink.set_property('wait-on-eos',False)
    for element in elements:pipeline.add(element)
    if not source.link(demux) or not parser.link(decoder) or not decoder.link(encoder) or not encoder.link(sink):
        raise RuntimeError('Cannot link complete-frame MPP/JPEG pipeline')
    def pad_added(element,pad):
        caps=pad.get_current_caps() or pad.query_caps(None)
        if caps and caps.to_string().startswith(('video/x-h265','video/x-h264')):
            target=parser.get_static_pad('sink')
            if not target.is_linked():pad.link(target)
    demux.connect('pad-added',pad_added)
    bus=pipeline.get_bus()
    def bus_error():
        message=bus.pop_filtered(Gst.MessageType.ERROR)
        if message:
            error,debug=message.parse_error()
            raise RuntimeError(str(error)+' / '+str(debug))
    try:
        pipeline.set_state(Gst.State.PAUSED)
        result,state,pending=pipeline.get_state(5*Gst.SECOND)
        bus_error()
        if result==Gst.StateChangeReturn.FAILURE:
            raise RuntimeError('Cannot preroll hardware playback')
        if args.seek_ns:
            # Seek to a preceding keyframe, then inspect original PTS while
            # walking forward. Never relabel a segment-clipped neighboring frame.
            flags=Gst.SeekFlags.FLUSH|Gst.SeekFlags.KEY_UNIT|Gst.SeekFlags.SNAP_BEFORE
            if not pipeline.seek_simple(Gst.Format.TIME,flags,args.seek_ns):
                print('Key-unit seek unavailable; walking from original preroll',file=sys.stderr,flush=True)
        pipeline.set_state(Gst.State.PLAYING)
        index=args.start_index
        while True:
            sample=sink.emit('try-pull-sample',Gst.SECOND//2)
            if sample is None:
                bus_error()
                if sink.get_property('eos'):break
                continue
            buffer=sample.get_buffer()
            pts=int(buffer.pts)
            if pts==Gst.CLOCK_TIME_NONE or pts>=2**63:
                raise RuntimeError('Decoded frame has no original PTS')
            actual_index=round((pts-args.pts_offset_ns)*args.fps/1e9)
            target=args.pts_offset_ns+round(actual_index*1e9/args.fps)
            if actual_index<0 or abs(pts-target)>args.pts_tolerance_ns:
                raise RuntimeError('Decoded frame does not match the CFR recording timeline')
            if actual_index<args.start_index:
                continue  # Explicit user seek preroll only; never ordinary play.
            if actual_index!=index:
                raise RuntimeError('Decoded original PTS indicates missing or repeated frame %d/%d'%(actual_index,index))
            ok,info=buffer.map(Gst.MapFlags.READ)
            if not ok:raise RuntimeError('Cannot map decoded JPEG frame')
            try:jpeg=bytes(info.data)
            finally:buffer.unmap(info)
            if not 4<=len(jpeg)<=MAX_JPEG:raise RuntimeError('Encoded replay JPEG exceeds limit')
            output.write(HEADER.pack(MAGIC,len(jpeg),actual_index,pts))
            output.write(jpeg)
            index+=1
    finally:
        pipeline.set_state(Gst.State.NULL)
        output.close()


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--worker',action='store_true',required=True)
    p.add_argument('--path',required=True)
    p.add_argument('--codec',choices=('hevc','h264'),required=True)
    p.add_argument('--width',type=int,required=True)
    p.add_argument('--height',type=int,required=True)
    p.add_argument('--quality',type=int,required=True)
    p.add_argument('--start-index',type=int,required=True)
    p.add_argument('--seek-ns',type=int,required=True)
    p.add_argument('--fps',type=float,required=True)
    p.add_argument('--pts-offset-ns',type=int,required=True)
    p.add_argument('--pts-tolerance-ns',type=int,required=True)
    args=p.parse_args()
    try:_worker(args)
    except BrokenPipeError:pass
    except Exception as error:
        print('Replay hardware decoder: '+str(error),file=sys.stderr,flush=True)
        raise SystemExit(2)
