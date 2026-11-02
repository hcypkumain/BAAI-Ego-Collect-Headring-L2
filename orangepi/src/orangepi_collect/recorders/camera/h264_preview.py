"""Bounded hardware JPEG preview fed by the existing H.264 owner."""
import queue
import threading
import time


class H264JpegPreview:
    def __init__(self, callback, width=1920, height=1200):
        import gi
        gi.require_version('Gst','1.0')
        from gi.repository import Gst
        Gst.init(None);self.Gst=Gst;self.callback=callback
        self.size=(width,height)
        self.stop=threading.Event();self.lock=threading.Lock()
        self.queue=queue.Queue(maxsize=16);self.await_idr=True
        self.frames=0;self.dropped=0;self.error=None;self.first_ts=None
        self.last_offer=self.last_frame=time.monotonic()
        self.pipeline=Gst.parse_launch(
            'appsrc name=input is-live=true format=time block=false max-bytes=2097152 '
            'caps="video/x-h264,stream-format=byte-stream,alignment=au,framerate=30/1" ! '
            'h264parse ! mppvideodec format=NV12 width=%d height=%d ignore-error=false ! '
            'queue max-size-buffers=2 max-size-bytes=0 max-size-time=0 leaky=downstream ! '
            'mppjpegenc quant=6 ! appsink name=output emit-signals=true sync=false max-buffers=2 drop=true'
            %(width,height))
        self.source=self.pipeline.get_by_name('input');self.bus=self.pipeline.get_bus()
        self.pipeline.get_by_name('output').connect('new-sample',self._sample)
        if self.pipeline.set_state(Gst.State.PLAYING)==Gst.StateChangeReturn.FAILURE:
            self.pipeline.set_state(Gst.State.NULL)
            raise RuntimeError('hardware headband preview cannot start')
        self.thread=threading.Thread(target=self._feed,name='h264-preview',daemon=True)
        self.thread.start()

    def offer(self,payload,ts_us,keyframe):
        # Called by a recording source: never wait for preview IO or decoding.
        if self.stop.is_set() or not self.lock.acquire(False):return
        try:
            if self.await_idr and not keyframe:return
            if self.queue.full():
                while True:
                    try:self.queue.get_nowait();self.dropped+=1
                    except queue.Empty:break
                self.await_idr=True
                if not keyframe:return
            self.await_idr=False
            self.queue.put_nowait((payload,int(ts_us)))
            self.last_offer=time.monotonic()
        finally:self.lock.release()

    def _feed(self):
        Gst=self.Gst
        try:
            while not self.stop.is_set():
                try:payload,stamp=self.queue.get(timeout=.1)
                except queue.Empty:continue
                if self.first_ts is None:self.first_ts=stamp
                if self.source.get_property('current-level-bytes')>2097152:
                    raise RuntimeError('hardware preview input stalled')
                buffer=Gst.Buffer.new_allocate(None,len(payload),None);buffer.fill(0,payload)
                buffer.pts=buffer.dts=max(0,(stamp-self.first_ts)*1000)
                buffer.duration=Gst.SECOND//30
                if self.source.emit('push-buffer',buffer)!=Gst.FlowReturn.OK and not self.stop.is_set():
                    raise RuntimeError('hardware preview rejected H.264 frame')
        except Exception as error:
            if not self.stop.is_set():self.error=str(error)

    def _sample(self,sink):
        sample=sink.emit('pull-sample')
        if sample is None:return self.Gst.FlowReturn.ERROR
        buffer=sample.get_buffer();payload=buffer.extract_dup(0,buffer.get_size())
        if not payload.startswith(b'\xff\xd8'):
            self.error='hardware preview did not output JPEG';return self.Gst.FlowReturn.ERROR
        self.frames+=1;self.last_frame=time.monotonic()
        try:self.callback(payload)
        except Exception as error:self.error=str(error)
        return self.Gst.FlowReturn.OK

    def health(self):
        message=self.bus.pop_filtered(self.Gst.MessageType.ERROR)
        if message:
            error,detail=message.parse_error();self.error='hardware preview: %s %s'%(error,detail)
        if self.error:raise RuntimeError(self.error)
        if time.monotonic()-self.last_frame>3 and time.monotonic()-self.last_offer<1:
            raise RuntimeError('hardware preview has no decoded frames for 3 seconds')

    def close(self):
        self.stop.set();self.thread.join(.5)
        # Drain the encoder before destroying its DMA buffer pool.
        self.source.emit('end-of-stream')
        self.bus.timed_pop_filtered(self.Gst.SECOND//2,self.Gst.MessageType.EOS|self.Gst.MessageType.ERROR)
        self.pipeline.set_state(self.Gst.State.NULL);self.thread.join(2)


class HeadbandPreviewSource:
    """Idle preview owns one subscription and releases it before preparation."""
    def __init__(self,name,callback,width=1920,height=1200):
        from orangepi_collect.recorders.camera.camd_source import CamdH264Source
        self.preview=H264JpegPreview(callback,width,height)
        try:self.input=CamdH264Source(name,lambda payload,ts,key,*arrival:self.preview.offer(payload,ts,key))
        except BaseException:self.preview.close();raise
    def health(self):
        self.input.health();self.preview.health()
        if time.monotonic()-self.input.last_frame>3:
            raise RuntimeError('headband preview receives no frames for 3 seconds')
    def close(self):self.input.close();self.preview.close()
