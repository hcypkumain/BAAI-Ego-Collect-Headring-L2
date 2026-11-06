"""CPU-only idle NV12 -> JPEG. Does not open Rockchip MPP/RGA devices."""
import ctypes as ct
import ctypes.util
import threading

from orangepi_collect.recorders.camera.snapshot_preview import SnapshotPreviewSource

class TurboNv12Jpeg:
    def __init__(self,quality=70):
        import numpy as np
        self.np=np;self.quality=quality;self.lock=threading.Lock()
        self.lib=ct.CDLL(ctypes.util.find_library('turbojpeg') or 'libturbojpeg.so.0')
        bytep=ct.POINTER(ct.c_ubyte)
        self.lib.tjInitCompress.argtypes=[];self.lib.tjInitCompress.restype=ct.c_void_p
        self.lib.tjDestroy.argtypes=[ct.c_void_p];self.lib.tjDestroy.restype=ct.c_int
        self.lib.tjFree.argtypes=[bytep];self.lib.tjFree.restype=None
        self.lib.tjCompressFromYUVPlanes.argtypes=[ct.c_void_p,ct.POINTER(bytep),ct.c_int,ct.POINTER(ct.c_int),ct.c_int,ct.c_int,ct.POINTER(bytep),ct.POINTER(ct.c_ulong),ct.c_int,ct.c_int]
        self.lib.tjCompressFromYUVPlanes.restype=ct.c_int
        self.handle=self.lib.tjInitCompress()
        if not self.handle:raise RuntimeError('CPU JPEG compressor cannot initialize')

    def __call__(self,width,height,payload,size):
        np=self.np
        if width<=0 or height<=0 or width%2 or height%2 or len(payload)!=width*height*3//2:raise ValueError('Invalid NV12 frame')
        outw=min(width,int(size[0]));outh=min(height,int(size[1]));outw-=outw%2;outh-=outh%2
        if min(outw,outh)<2:raise ValueError('Invalid JPEG display size')
        raw=np.frombuffer(payload,dtype=np.uint8);y=raw[:width*height].reshape(height,width);uv=raw[width*height:].reshape(height//2,width//2,2)
        if (outw,outh)!=(width,height):
            rows=np.arange(outh,dtype=np.intp)*height//outh;cols=np.arange(outw,dtype=np.intp)*width//outw
            y=np.ascontiguousarray(y[rows[:,None],cols[None,:]])
            cr=np.arange(outh//2,dtype=np.intp)*(height//2)//(outh//2);cc=np.arange(outw//2,dtype=np.intp)*(width//2)//(outw//2)
            uv=uv[cr[:,None],cc[None,:],:]
        u=np.ascontiguousarray(uv[:,:,0]);v=np.ascontiguousarray(uv[:,:,1]);bytep=ct.POINTER(ct.c_ubyte)
        planes=(bytep*3)(y.ctypes.data_as(bytep),u.ctypes.data_as(bytep),v.ctypes.data_as(bytep));strides=(ct.c_int*3)(int(y.strides[0]),outw//2,outw//2)
        output=bytep();length=ct.c_ulong(0)
        with self.lock:
            if not self.handle:raise RuntimeError('CPU JPEG compressor closed')
            try:
                result=self.lib.tjCompressFromYUVPlanes(self.handle,planes,outw,strides,outh,2,ct.byref(output),ct.byref(length),self.quality,2048)
                if result!=0:raise RuntimeError('CPU planar JPEG compression failed')
                if not 0<length.value<=2*1024*1024:raise RuntimeError('CPU JPEG exceeds display bound')
                return ct.string_at(output,length.value)
            finally:
                if output:self.lib.tjFree(output)

    def close(self):
        with self.lock:
            if self.handle:self.lib.tjDestroy(self.handle);self.handle=None

class SoftwareHeadbandPreviewSource(SnapshotPreviewSource):
    def __init__(self,name,callback,width=1920,height=1200):
        self.encoder=TurboNv12Jpeg()
        try:super().__init__(name,callback,width,height,encoder=self.encoder,interval=1/30)
        except BaseException:self.encoder.close();raise

    def close(self):
        super().close()
        # A failed snapshot can finish late; never free an in-use compressor.
        if not self.thread.is_alive():self.encoder.close()
