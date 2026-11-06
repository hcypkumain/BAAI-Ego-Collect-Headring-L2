"""Short display leases; affects derived previews only, never raw capture."""
import json,os,threading,time
from pathlib import Path
SIGNALS=frozenset(('emg_left','emg_right'))
CAMERAS=frozenset(('cam_left_wrist','cam_right_wrist','cam_ros_left','cam_ros_right','cam_ros_bleft','cam_ros_bright'))

class DisplayDemand:
    def __init__(self,directory):
        self.path=Path(directory)/'display-demand.json';self.lock=threading.Lock();self.clients={};self.force_camera=False;self.force_signals=();self.blocked=False;self.last=0;self.previous=None
        self._write(time.monotonic(),True)
    def _write(self,now,force=False):
        self.clients={k:v for k,v in self.clients.items() if v[0]>now}
        signals=sorted(set(self.force_signals)|{n for expiry,names,camera,selected in self.clients.values() for n in names})
        camera_names=sorted(CAMERAS if self.force_camera else {n for expiry,names,camera,selected in self.clients.values() if camera for n in (CAMERAS if selected is None else selected)})
        detail_names=sorted({n for expiry,names,camera,selected in self.clients.values() if camera and selected is not None for n in selected})
        cameras=bool(camera_names);key=(tuple(signals),cameras,tuple(camera_names),tuple(detail_names))
        if not force and key==self.previous and now-self.last<1:return
        value=dict(signals=signals,cameras=cameras,camera_names=camera_names,camera_detail_names=detail_names,until=now+3)
        tmp=self.path.with_suffix('.tmp');tmp.write_text(json.dumps(value,separators=(',',':')));os.replace(str(tmp),str(self.path));self.previous=key;self.last=now
    def touch(self,client,names,cameras=False,camera_names=None):
        with self.lock:
            if self.blocked:names=();cameras=False
            if client not in self.clients and len(self.clients)>=32:self.clients.pop(next(iter(self.clients)))
            selected=None if camera_names is None else tuple(CAMERAS.intersection(camera_names))
            self.clients[client]=(time.monotonic()+3,tuple(n for n in names if n in SIGNALS),bool(cameras),selected);self._write(time.monotonic())
    def release(self,client):
        with self.lock:
            self.clients.pop(client,None);self._write(time.monotonic(),True)
    def state(self,value):
        with self.lock:
            state=value.get('state','');self.force_camera=state in ('preparing','ready') or state.startswith('wrist_')
            # Calibration consumes these envelopes even when no browser is
            # displaying a waveform. Viewer leases must not stop that evidence.
            self.force_signals=('emg_left','emg_right') if state in ('emg_pairing','emg_manual') else ()
            self.blocked=state in ('recording','countdown') or value.get('display_blocked') is True
            if self.blocked:self.clients.clear();self.force_camera=False;self.force_signals=()
            self._write(time.monotonic())

class DemandReader:
    def __init__(self,directory):self.path=Path(directory)/'display-demand.json';self.next=0;self.signals=SIGNALS;self.cameras=True;self.camera_names=CAMERAS;self.camera_detail_names=frozenset();self.until=0
    def refresh(self,now=None):
        now=time.monotonic() if now is None else now
        if now<self.next:return
        self.next=now+.25
        try:
            raw=self.path.read_bytes()
            if len(raw)>1024:raise ValueError()
            value=json.loads(raw);self.until=float(value['until']);live=now<self.until<now+4
            self.signals=frozenset(value['signals'])&SIGNALS if live else frozenset();self.cameras=live and value.get('cameras') is True
            self.camera_names=CAMERAS.intersection(value.get('camera_names',CAMERAS)) if self.cameras else frozenset()
            self.camera_detail_names=CAMERAS.intersection(value.get('camera_detail_names',())) if self.cameras else frozenset()
        except FileNotFoundError:self.signals=SIGNALS;self.cameras=True;self.camera_names=CAMERAS;self.camera_detail_names=frozenset()
        except (OSError,ValueError,TypeError,KeyError):self.signals=frozenset();self.cameras=False;self.camera_names=frozenset();self.camera_detail_names=frozenset()
