"""Idle SNAPSHOT -> hardware JPEG, with process-scoped MPP resources."""
import os
from pathlib import Path
import queue
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

HEADER=struct.Struct('!cI')
MAX_FRAME=2*1024*1024

class IsolatedHeadbandPreviewSource:
    def __init__(self,name,callback,width=1920,height=1200):
        from orangepi_collect.recorders.camera import camd_source
        self.size=(width,height);self.preview=self
        self.frames=0;self.dropped=0;self.error=None
        self.last_frame=time.monotonic();self.stop=threading.Event()
        self._socket,child=socket.socketpair();self._socket.settimeout(.5)
        source_root=str(Path(camd_source.__file__).resolve().parents[3])
        args=[sys.executable,str(Path(__file__).resolve()),'--child','--fd',str(child.fileno()),
              '--camera',name,'--width',str(width),'--height',str(height),
              '--source-root',source_root,'--parent',str(os.getpid())]
        try:
            self.process=subprocess.Popen(args,pass_fds=(child.fileno(),),
                stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        except BaseException:
            self._socket.close();raise
        finally:child.close()
        self.thread=threading.Thread(target=self._read,args=(callback,),
            name='isolated-jpeg-'+name,daemon=True);self.thread.start()

    def _exact(self,size):
        data=bytearray()
        while len(data)<size:
            if self.stop.is_set():raise EOFError()
            try:part=self._socket.recv(size-len(data))
            except socket.timeout:continue
            if not part:raise EOFError()
            data.extend(part)
        return bytes(data)

    def _read(self,callback):
        try:
            while not self.stop.is_set():
                kind,size=HEADER.unpack(self._exact(HEADER.size))
                if not 0<size<=MAX_FRAME:raise RuntimeError('Invalid child frame length')
                payload=self._exact(size)
                if kind==b'E':raise RuntimeError(payload.decode('utf-8','replace')[:1000])
                if kind!=b'J' or not payload.startswith(b'\xff\xd8'):raise RuntimeError('Invalid child JPEG')
                if self.stop.is_set():break
                callback(payload);self.frames+=1;self.last_frame=time.monotonic()
        except Exception as error:
            if not self.stop.is_set():self.error=str(error) or type(error).__name__

    def health(self):
        if self.error:raise RuntimeError('isolated hardware preview: '+self.error)
        code=self.process.poll()
        if code is not None:raise RuntimeError('isolated hardware preview exited: '+str(code))
        if time.monotonic()-self.last_frame>4:raise RuntimeError('isolated preview has no fresh frame for 4 seconds')

    def close(self):
        if self.stop.is_set():return
        self.stop.set()
        if self.process.poll() is None:self.process.terminate()
        try:self._socket.shutdown(socket.SHUT_RDWR)
        except OSError:pass
        self._socket.close();self.thread.join(.6)
        try:self.process.wait(timeout=.8)
        except subprocess.TimeoutExpired:
            self.process.kill();self.process.wait(timeout=1)

def child_main(args):
    sys.path.insert(0,args.source_root)
    # The encoder may survive its parent briefly: hold its own host lock until
    # process exit, before it touches camd or initializes the pipeline.
    from orangepi_collect.common.acquisition_time_lock import acquire
    acquire()
    # The child must not outlive the preview owner, even after a parent crash.
    import ctypes
    libc=ctypes.CDLL(None,use_errno=True)
    if libc.prctl(1,signal.SIGTERM,0,0,0)!=0:raise OSError(ctypes.get_errno(),'prctl')
    if os.getppid()!=args.parent:return
    stop=threading.Event();signal.signal(signal.SIGTERM,lambda *a:stop.set())
    signal.signal(signal.SIGINT,lambda *a:stop.set())
    def parent_watchdog():
        while not stop.wait(.2):
            if os.getppid()!=args.parent:stop.set()
        if os.getppid()!=args.parent:
            # Avoid an orphan if a native codec blocks during cleanup.
            time.sleep(.8);os._exit(0)
    threading.Thread(target=parent_watchdog,name='preview-parent',daemon=True).start()
    channel=socket.socket(fileno=args.fd);channel.settimeout(.5)
    pending=queue.Queue(maxsize=1);send_lock=threading.Lock()
    def send(kind,payload):
        with send_lock:channel.sendall(HEADER.pack(kind,len(payload))+payload)
    def offer(payload):
        if stop.is_set():return
        if not isinstance(payload,bytes) or not 0<len(payload)<=MAX_FRAME:raise RuntimeError('JPEG exceeds preview bound')
        try:pending.put_nowait(payload)
        except queue.Full:
            try:pending.get_nowait()
            except queue.Empty:pass
            try:pending.put_nowait(payload)
            except queue.Full:pass
    def writer():
        while not stop.is_set():
            try:payload=pending.get(timeout=.1)
            except queue.Empty:continue
            try:send(b'J',payload)
            except OSError:stop.set();return
    worker=threading.Thread(target=writer,name='latest-jpeg',daemon=True);worker.start()
    pipeline=None
    try:
        import gi
        gi.require_version('Gst','1.0')
        from gi.repository import Gst
        from orangepi_collect.recorders.camera.camd_source import snapshot
        Gst.init(None)
        width,height,stamp,raw=snapshot(args.camera,timeout=3)
        # Encode native NV12 directly. No H.264 subscription/decoder is opened,
        # and no full-resolution numpy/Pillow conversion runs in the producer.
        pipeline=Gst.parse_launch(
            'appsrc name=input is-live=true format=time block=false max-bytes=6912000 '
            'caps="video/x-raw,format=NV12,width=%d,height=%d,framerate=30/1" ! '
            'queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream ! '
            'mppjpegenc quant=6 width=%d height=%d ! appsink name=output emit-signals=true sync=false max-buffers=1 drop=true'
            %(width,height,min(width,args.width),min(height,args.height)))
        source=pipeline.get_by_name('input');bus=pipeline.get_bus()
        def sample(sink):
            item=sink.emit('pull-sample')
            if item is None:return Gst.FlowReturn.ERROR
            buffer=item.get_buffer();payload=buffer.extract_dup(0,buffer.get_size())
            offer(payload);return Gst.FlowReturn.OK
        pipeline.get_by_name('output').connect('new-sample',sample)
        if pipeline.set_state(Gst.State.PLAYING)==Gst.StateChangeReturn.FAILURE:raise RuntimeError('NV12 hardware encoder cannot start')
        first=stamp;previous=None;last=time.monotonic()
        while not stop.is_set():
            started=time.monotonic()
            width2,height2,stamp,raw=snapshot(args.camera,timeout=3)
            if (width2,height2)!=(width,height):raise RuntimeError('Raw preview geometry changed')
            if stamp!=previous:
                if source.get_property('current-level-bytes')<=6912000:
                    buffer=Gst.Buffer.new_allocate(None,len(raw),None);buffer.fill(0,raw)
                    buffer.pts=buffer.dts=max(0,(stamp-first)*1000);buffer.duration=Gst.SECOND//30
                    if source.emit('push-buffer',buffer)!=Gst.FlowReturn.OK:raise RuntimeError('NV12 hardware encoder rejected frame')
                    last=time.monotonic();previous=stamp
            message=bus.pop_filtered(Gst.MessageType.ERROR)
            if message:
                error,detail=message.parse_error();raise RuntimeError(str(error))
            if time.monotonic()-last>3:raise RuntimeError('SNAPSHOT has no fresh raw frame')
            if stop.wait(max(.002,1/33-(time.monotonic()-started))):break
    except Exception as error:
        if not stop.is_set():
            try:send(b'E',str(error).encode('utf-8','replace')[:1000] or b'preview_failure')
            except OSError:pass
    finally:
        stop.set()
        # SNAPSHOT has no persistent recording subscription to release.
        try:channel.close()
        except OSError:pass
        # All MPP/GStreamer descriptors belong exclusively to this child.
        # Avoid the encoder's slow/leaky in-process EOS teardown on every
        # switch: after releasing camd, process exit reclaims the whole pool.
        os._exit(0)

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--child',action='store_true');p.add_argument('--fd',type=int)
    p.add_argument('--camera');p.add_argument('--width',type=int);p.add_argument('--height',type=int)
    p.add_argument('--source-root');p.add_argument('--parent',type=int);args=p.parse_args()
    if not args.child:p.error('This module is an idle-preview worker, not a recorder.')
    child_main(args)
