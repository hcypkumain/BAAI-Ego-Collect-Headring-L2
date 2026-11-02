"""USB-only owner with control semantics aligned to OmniBCI SDK 843013ba.

One owner reads both EEG and control frames. Importing never opens a device.
No automatic measurement: begin_impedance must be explicitly requested.
The public OmniBCI decoder is reused for EEG conversion, not serial ownership.
"""
from collections import deque
from dataclasses import dataclass, replace
from functools import reduce
import binascii
import math
import operator
import time


@dataclass(frozen=True)
class Ack:
    command: int
    argument: int
    register_value: int
    loff_p: int
    loff_n: int
    loff_config: int
    reference: int
    mode: int
    verified: bool
    enabled_mask: int


class FrameParser:
    """Prioritize complete CRC-valid EEG frames; never scan their payload as ACK."""
    def __init__(self):
        self.buffer = bytearray()
        self.crc_errors = self.discarded = 0

    def feed(self, data):
        self.buffer.extend(data)
        events = []
        b = self.buffer
        while b:
            if b[0] == 0xa5:
                if len(b) < 4: break
                if b[:3] == b'\xa5\x5a\x01' and b[3] in (1, 2):
                    if len(b) < 48: break
                    frame = bytes(b[:48])
                    if binascii.crc_hqx(frame[:46], 0xffff) == int.from_bytes(frame[46:], 'little'):
                        events.append(('eeg' if frame[3] == 1 else 'trigger', frame))
                        del b[:48]
                        continue
                    self.crc_errors += 1
            elif b[0] == 0xbc:
                if len(b) < 2: break
                length = 18 if b[1] == 0x43 else 8 if b[1] == 0x48 else 12
                if len(b) < length: break
                frame = bytes(b[:length])
                if reduce(operator.xor, frame, 0) == 0:
                    if length == 18:
                        ref, mode, enabled, bias, srb2 = frame[3:8]
                        gains = tuple(frame[8:16])
                        if (frame[2] == 1 and ref < 2 and mode < 5 and frame[16] < 2
                                and not (bias | srb2) & ~enabled
                                and all(g in (1, 2, 4, 6, 8, 12, 24) for g in gains)):
                            events.append(('config', dict(reference=ref, mode=mode,
                                enabled_mask=enabled, bias_mask=bias, srb2_mask=srb2,
                                gains=gains, verified=bool(frame[16]))))
                            del b[:length]
                            continue
                    elif length == 8:
                        if frame[2] == 1:
                            events.append(('heartbeat', frame))
                            del b[:length]
                            continue
                    elif frame[7] < 2 and frame[8] < 5:
                        events.append(('ack', Ack(*frame[1:9], bool(frame[9] & 1), frame[10])))
                        del b[:length]
                        continue
            self.discarded += 1
            del b[:1]
        return events


def loff_matches(ack, mask, config):
    """Require the current transaction and ALL observed LOFF readback fields."""
    ref = int(config.reference == 'srb2')
    mode = ('both_bias', 'eeg', 'no_bias', 'shorted', 'test').index(config.mode)
    expected_p = mask if not ref else 0
    expected_n = mask if ref else 0
    return (ack.verified and ack.command == 0xa9 and ack.argument == mask
        and ack.register_value == mask and ack.loff_p == expected_p
        and ack.loff_n == expected_n and ack.loff_config == (2 if mask else 0)
        and ack.reference == ref and ack.mode == mode
        and ack.enabled_mask == config.enabled_mask)


class NativeSerial:
    """Preserve current baud/line settings. USB CDC only; restore local settings."""
    def __init__(self, port):
        import os
        self.windows = os.name == 'nt'
        self.closed = False
        if self.windows:
            import ctypes as c
            from ctypes import wintypes as w
            self.c, self.w = c, w
            self.k = k = c.WinDLL('kernel32', use_last_error=True)
            k.CreateFileW.argtypes=[w.LPCWSTR,w.DWORD,w.DWORD,c.c_void_p,w.DWORD,w.DWORD,w.HANDLE]
            k.CreateFileW.restype=w.HANDLE
            k.CloseHandle.argtypes=[w.HANDLE]; k.CloseHandle.restype=w.BOOL
            k.ReadFile.argtypes=[w.HANDLE,c.c_void_p,w.DWORD,c.POINTER(w.DWORD),c.c_void_p]
            k.ReadFile.restype=w.BOOL
            k.WriteFile.argtypes=k.ReadFile.argtypes; k.WriteFile.restype=w.BOOL
            class Timeouts(c.Structure):
                _fields_=[(name,w.DWORD) for name in ('interval','rm','rc','wm','wc')]
            self.Timeouts = Timeouts
            k.GetCommTimeouts.argtypes=[w.HANDLE,c.POINTER(Timeouts)]; k.GetCommTimeouts.restype=w.BOOL
            k.SetCommTimeouts.argtypes=k.GetCommTimeouts.argtypes; k.SetCommTimeouts.restype=w.BOOL
            self.handle=k.CreateFileW('\\\\.\\'+port,0xc0000000,0,None,3,0,None)
            if self.handle==c.c_void_p(-1).value: raise c.WinError(c.get_last_error())
            self.original=Timeouts()
            if not k.GetCommTimeouts(self.handle,c.byref(self.original)):
                k.CloseHandle(self.handle); raise c.WinError(c.get_last_error())
        else:
            import fcntl, termios, tty
            self.os, self.termios, self.fcntl = os, termios, fcntl
            self.fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            self.original = None
            self.exclusive = False
            try:
                fcntl.ioctl(self.fd, termios.TIOCEXCL)
                self.exclusive = True
                self.original = termios.tcgetattr(self.fd)
                tty.setraw(self.fd, termios.TCSANOW)
            except BaseException:
                try: self._close_posix()
                except Exception: pass  # Preserve the original initialization failure.
                self.closed = True
                raise

    def read(self, timeout):
        if self.windows:
            c,w,k=self.c,self.w,self.k
            limits=self.Timeouts(0xffffffff,0,max(1,round(timeout*1000)),0,1000)
            if not k.SetCommTimeouts(self.handle,c.byref(limits)): raise c.WinError(c.get_last_error())
            buf=c.create_string_buffer(65536); n=w.DWORD()
            if not k.ReadFile(self.handle,buf,len(buf),c.byref(n),None): raise c.WinError(c.get_last_error())
            return buf.raw[:n.value]
        import select
        if not select.select([self.fd],[],[],timeout)[0]: return b''
        try: return self.os.read(self.fd,65536)
        except BlockingIOError: return b''

    def write(self, payload):
        if self.windows:
            c,w,k=self.c,self.w,self.k
            buf=c.create_string_buffer(payload); n=w.DWORD()
            if not k.WriteFile(self.handle,buf,len(payload),c.byref(n),None): raise c.WinError(c.get_last_error())
            if n.value!=len(payload): raise OSError('partial serial command write')
        else:
            import select
            view=memoryview(payload); end=time.monotonic()+1
            while view:
                remaining=end-time.monotonic()
                if remaining<=0 or not select.select([],[self.fd],[],remaining)[1]: raise TimeoutError('serial write timeout')
                try: view=view[self.os.write(self.fd,view):]
                except BlockingIOError: continue

    def _close_posix(self):
        try:
            if self.original is not None:
                self.termios.tcsetattr(self.fd,self.termios.TCSANOW,self.original)
        finally:
            try:
                if self.exclusive:
                    self.fcntl.ioctl(self.fd,self.termios.TIOCNXCL)
                    self.exclusive = False
            finally: self.os.close(self.fd)

    def close(self):
        if self.closed: return
        self.closed=True
        if self.windows:
            try:
                if not self.k.SetCommTimeouts(self.handle,self.c.byref(self.original)):
                    raise self.c.WinError(self.c.get_last_error())
            finally: self.k.CloseHandle(self.handle)
        else: self._close_posix()


class SerialV1Board:
    """Opt-in single-owner experimental replacement for serial Board only.

    Uses public decode_frames for EEG. BLE, metadata discovery, trigger delivery,
    arbitrary mode changes and automatic reconnection are intentionally unsupported.
    This backend must pass a controlled hardware acceptance before production use.
    """
    def __init__(self, transport, timeout=5):
        self.transport=transport; self.parser=FrameParser(); self.frames=deque()
        self._config=None; self.generation=0; self.running=False; self.measurement=False
        self.original_config=None; self.restore_required=False; self.samples=0
        self.closed=False; self.fault_error=None; self.stopping=False; self.last_stop=None
        self.notification_error=None; self.notification_error_count=0
        self.startup_diagnostics={'config_queries':0,'start_commands':0}
        self._last_sequence=None; self._index=0; self.missing=0
        self.on_state=lambda **state: None
        self._query_config(timeout)

    @classmethod
    def connect(cls, endpoint, timeout=5):
        if not endpoint.startswith('serial://'): raise ValueError('experimental backend supports serial USB only')
        transport=NativeSerial(endpoint[len('serial://'):])
        try: return cls(transport,timeout)
        except Exception: transport.close(); raise

    def __enter__(self):
        self._ensure_open()
        return self
    def __exit__(self,kind,error,traceback):
        try: self.close()
        except Exception as cleanup:
            if error is None: raise
            if hasattr(error,'add_note'): error.add_note('Serial cleanup also failed: '+str(cleanup))
    @property
    def info(self): return None  # Do not invent identity from a previous log.
    @property
    def snapshot(self):
        from omnibci.api import Snapshot
        return Snapshot(state='faulted' if self.fault_error else 'closed' if self.closed else
            'stopping' if self.stopping else 'measuring' if self.measurement else 'streaming' if self.running else 'ready',
            config=self._config,generation=self.generation,samples=self.samples,
            missing_samples=self.missing,crc_errors=self.parser.crc_errors,
            metadata=None,error=self.fault_error,ble={})

    def get_config(self): return self._config

    def _ensure_open(self):
        if self.closed: raise RuntimeError('serial session is closed; reconnect before another acquisition')

    def _notify(self, **state):
        """Observers must never preempt STOP/LOFF shutdown/config restoration.

        A parent pipe or status callback may disappear during recovery. Keep
        that failure distinct from the original hardware error, and report it
        on a later successful notification without claiming delivery succeeded.
        """
        if self.notification_error:
            state=dict(state,notification_error=self.notification_error,
                       notification_error_count=self.notification_error_count)
        try:
            self.on_state(**state)
            return True
        except BaseException as error:
            try: detail=type(error).__name__+': '+str(error)[:240]
            except BaseException: detail='status observer failed'
            self.notification_error=detail
            self.notification_error_count=min(2147483647,self.notification_error_count+1)
            return False

    def _isolate(self, error):
        """Never permit normal acquisition after uncertain control restoration."""
        self.fault_error=str(error); self.running=False; self.measurement=False; self.stopping=False
        self.frames.clear(); self.closed=True
        try: self.transport.close()
        except Exception as cleanup:
            self.fault_error += '; close: '+str(cleanup)
        self._notify(phase='fault',error=self.fault_error,active=False,pending=False,
            deadline_monotonic=None,session_closed=True)

    def _events(self, timeout):
        return self.parser.feed(self.transport.read(max(.001,timeout)))

    def _wait(self, predicate, timeout):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            matched=None
            events=self._events(min(.05,deadline-time.monotonic()))
            if time.monotonic()>=deadline: break
            for kind,value in events:
                if kind=='eeg' and self.running: self.frames.append(value)
                if predicate(kind,value): matched=value
            if matched is not None: return matched
        raise TimeoutError('device confirmation timeout')

    def _query_config(self, timeout):
        from omnibci import FrontendConfig
        # A freshly enumerated ESP32 can still be booting when the first query
        # arrives. Reissue only the idempotent read command inside one budget.
        # Never reset the USB hub, frontend, or an active recording here.
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            self.startup_diagnostics['config_queries']+=1
            self.transport.write(b'c')
            try:value=self._wait(lambda kind,v:kind=='config' and v['verified'],min(1.0,max(.001,deadline-time.monotonic())))
            except TimeoutError:
                if time.monotonic()>=deadline:
                    raise TimeoutError('EEG configuration handshake timed out after %d queries; crc=%d discarded=%d' %
                        (self.startup_diagnostics['config_queries'],self.parser.crc_errors,self.parser.discarded))
                continue
            self._config=FrontendConfig._from_wire(value)
            return self._config
        raise TimeoutError('EEG configuration handshake deadline expired')

    def configure(self, config, timeout=1.8):
        self._ensure_open()
        if self.running: raise RuntimeError('stop before configure')
        if self._config and config.mode!=self._config.mode:
            raise ValueError('experimental backend does not implement mode-changing commands')
        wire=config._wire()
        payload=bytes([0xa5,wire['reference'],config.enabled_mask,config.bias_mask,config.srb2_mask,*config.gains])
        self.transport.write(payload); self.transport.write(b'c')
        expected=dict(wire,verified=True); expected['gains']=tuple(expected['gains'])
        self._wait(lambda kind,v:kind=='config' and v==expected,timeout)
        self._config=replace(config,verified=True)

    def _loff(self, mask, timeout=1.8):
        self._ensure_open()
        self.transport.write(bytes([0xa9,mask]))
        return self._wait(lambda kind,v:kind=='ack' and loff_matches(v,mask,self._config),timeout)

    def start(self, timeout=3):
        self._ensure_open()
        if self.running: return
        self.frames.clear(); self.generation+=1; self._last_sequence=None; self._index=0
        self.running=True;deadline=time.monotonic()+timeout
        try:
            while time.monotonic()<deadline:
                self.startup_diagnostics['start_commands']+=1
                self.transport.write(b'b')
                try:self._wait(lambda kind,v:kind=='eeg',min(1.0,max(.001,deadline-time.monotonic())));return
                except TimeoutError:
                    if time.monotonic()>=deadline:raise
            raise TimeoutError('EEG start handshake deadline expired')
        except Exception:
            self.running=False
            self.transport.write(b's')
            raise

    def stop(self, timeout=3):
        self._ensure_open()
        if isinstance(timeout,bool) or not isinstance(timeout,(int,float)) or not math.isfinite(timeout) or timeout<=0:
            raise ValueError('stop timeout must be a positive finite number')
        # Shipped SDK ESP32 serial worker 18007c5f0: STOP is host I/O drain,
        # not a firmware ACK. Every raw receive resets the 30 ms quiet timer;
        # the worker also completes at its absolute 300 ms cap, even if data
        # keep arriving. Preserve that limitation in explicit diagnostics.
        caller_end=time.monotonic()+float(timeout); drained=0
        self.stopping=True; self.frames.clear()
        try:
            self.transport.write(b's')
            begin=time.monotonic(); cap=begin+.3; quiet=begin+.03
            while True:
                now=time.monotonic()
                if now>=caller_end: raise TimeoutError('serial STOP host drain timed out')
                if now>=quiet or now>=cap: break
                raw=self.transport.read(max(.000001,min(.02,quiet-now,cap-now,caller_end-now)))
                self.parser.feed(raw)
                if raw:
                    drained+=len(raw); quiet=time.monotonic()+.03
            now=time.monotonic()
            self.last_stop=dict(basis='sdk_serial_host_drain_30ms_quiet_300ms_cap',
                quiet_reached=now>=quiet,max_drain_reached=now>=cap,
                drained_bytes=drained,hardware_ack=False)
            self.running=False; self.stopping=False; self.frames.clear()
        except Exception as error:
            self._isolate(error)
            raise

    def begin_impedance(self, mask, timeout=1.8):
        self._ensure_open()
        if self.restore_required: raise RuntimeError('previous measurement not restored')
        cfg=self.get_config()
        if cfg.mode not in ('both_bias','eeg','no_bias') or not cfg.verified:
            raise ValueError('confirmed EEG configuration required')
        if type(mask) is not int or not 0<=mask<=255:
            raise ValueError('measurement mask must be an 8-bit integer')
        mask &= cfg.enabled_mask
        if not mask: raise ValueError('measurement mask must select an enabled channel')
        self.original_config=cfg; self.restore_required=True
        try:
            self._notify(phase='starting',active=False,pending=True,deadline_monotonic=time.monotonic()+8)
            self.stop()
            self.configure(replace(cfg,bias_mask=0),timeout)
            self._loff(mask,timeout)
            self.measurement=True
            self.start()
            self._notify(phase='measuring',active=True,pending=False,deadline_monotonic=None,
                          loff_off_confirmed=False,frontend_restored=False)
        except Exception as exc:
            try: self.end_impedance(timeout)
            except Exception as cleanup: raise RuntimeError(str(exc)+'; recovery: '+str(cleanup)) from exc
            raise

    def end_impedance(self, timeout=1.8):
        if not self.restore_required: return
        self._ensure_open()
        off=False; restored=False
        self._notify(phase='restoring',active=False,pending=True,deadline_monotonic=time.monotonic()+8)
        try:
            self.stop(); self._loff(0,timeout); off=True
            self._notify(loff_off_confirmed=True)
            self.configure(self.original_config,timeout); restored=True
            self.measurement=False; self.restore_required=False
            self._notify(phase='restored',active=False,pending=False,deadline_monotonic=None,
                          loff_off_confirmed=True,frontend_restored=True)
        except Exception as exc:
            problems=[]
            if not self.closed and not self.running:
                if not off:
                    try: self._loff(0,timeout); off=True
                    except Exception as cleanup: problems.append('LOFF shutdown: '+str(cleanup))
                if not restored:
                    try: self.configure(self.original_config,timeout); restored=True
                    except Exception as cleanup: problems.append('frontend restore: '+str(cleanup))
            if off and restored:
                self.measurement=False; self.restore_required=False
            else:
                detail=str(exc)+(('; recovery: '+'; '.join(problems)) if problems else '')
                self._isolate(detail)
            self._notify(phase='fault',error=str(exc),active=False,pending=False,
                          deadline_monotonic=None,loff_off_confirmed=off,frontend_restored=restored,
                          session_closed=self.closed)
            if problems and hasattr(exc,'add_note'): exc.add_note('; '.join(problems))
            raise

    def read(self, samples=None, timeout=.25):
        self._ensure_open()
        from omnibci import decode_frames, PartialReadError
        import numpy as np
        end=time.monotonic()+timeout
        needed=samples if samples is not None else 1
        while len(self.frames)<needed and time.monotonic()<end:
            for kind,value in self._events(min(.05,end-time.monotonic())):
                if kind=='eeg': self.frames.append(value)
        take=min(len(self.frames),samples) if samples is not None else len(self.frames)
        if not take:
            if samples is not None: raise PartialReadError(None)
            return None
        batch=decode_frames(b''.join(self.frames.popleft() for _ in range(take)),gains=self._config.gains)
        positions=[]
        for value in batch.sequence:
            sequence=int(value)
            if self._last_sequence is not None:
                delta=(sequence-self._last_sequence)&0xffffffff
                if not 0<delta<1000000: raise RuntimeError('non-forward EEG sequence; restart measurement epoch')
                self._index+=delta; self.missing+=delta-1
            positions.append(self._index); self._last_sequence=sequence
        self.samples+=len(batch)
        indices=np.frombuffer(np.asarray(positions,dtype='<u8').tobytes(),dtype='<u8')
        batch=replace(batch,sample_indices=indices,generation=self.generation,received_at=time.monotonic())
        if samples is not None and len(batch)<samples: raise PartialReadError(batch)
        return batch

    def close(self, timeout=5):
        if self.closed: return
        try:
            if self.restore_required: self.end_impedance(min(timeout,1.8))
            elif self.running: self.stop()
        finally:
            self.closed=True; self.running=False; self.measurement=False; self.stopping=False
            self.transport.close()
