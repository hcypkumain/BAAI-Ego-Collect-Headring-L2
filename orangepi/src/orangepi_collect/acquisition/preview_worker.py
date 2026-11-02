"""Controller-owned idle preview; released before scans, calibration or recording."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'common'))
import project_paths
import copy
import fcntl
import json
import os
import select
import signal
import termios
import threading
import time
from live_preview import publisher
from orangepi_collect.common import wrist_camera_settings


def write_wrist_report(name, report):
    directory = os.environ.get('ORANGEPI_PREVIEW_DIR')
    if directory:
        wrist_camera_settings.write_report(Path(directory) / ('wrist-controls-' + name + '.json'), report)


def preview_packets(buffer):
    position=0;packets=[]
    while len(buffer)-position>=3:
        found=buffer.find(b'\xd2\xd2\xd2',position)
        if found<0:position=max(position,len(buffer)-2);break
        position=found
        if len(buffer)-position<29:break
        if buffer[position+3] not in (0xaa,0xbb):position+=1;continue
        packets.append(bytes(buffer[position:position+29]));position+=29
    if position:del buffer[:position]
    return packets


PREVIEW_SCALE = 4
# This is the single pacer for headband previews: four concurrent SNAPSHOT+JPEG
# conversions cost ~135 ms per round, so 0.12 s asks for about as much as the
# board can deliver. live_preview.CAMERA_PUBLISH_INTERVAL is only a guard rail
# against a runaway producer and must stay well below this value -- two gates of
# similar size quantise against each other and halve the delivered rate.
PREVIEW_INTERVAL = 0.12
# camd STATUS is cheap but not free; cache the ownership answer briefly.
OWNERSHIP_TTL = 1.0
# camd answers a SNAPSHOT for a camera that stopped producing frames with
# "快照超时" after about two seconds. Sampling the cameras one after another
# therefore lets a single failed camera stall the whole preview round, which
# looked like "the preview froze" even though three cameras were healthy. Each
# camera gets its own thread, and a camera that keeps failing is backed off so
# it neither blocks the others nor hammers camd.
PREVIEW_FETCH_TIMEOUT = 3.0
PREVIEW_FAILURE_BACKOFF = 10.0


def _capture_owns_cameras(camd_source, state, now):
    """True when a capture already holds camd's per-camera H.264 subscriptions."""
    if now - state.get('owner_checked', -OWNERSHIP_TTL) < OWNERSHIP_TTL:
        return state.get('owner_busy', False)
    state['owner_checked'] = now
    try:
        cams = (camd_source.status().get('cams') or {}).values()
        state['owner_busy'] = any(cam.get('h264_stream') for cam in cams)
    except Exception:
        # If camd cannot be queried, let the snapshot attempt report the error.
        state['owner_busy'] = False
    return state['owner_busy']


def _pump_snapshot_previews(config, pub, state, now=None):
    """Sample headband frames through camd SNAPSHOT while a viewer is watching.

    This build of camd has no STREAM_JPEG, and holding a second STREAM_H264
    subscription would both collide with the recorder and still need decoding.
    SNAPSHOT works in any camd state, costs ~23 ms per frame at scale=4, and
    never competes with the recorder's subscription.
    """
    from orangepi_collect.recorders.camera import camd_source
    now = time.monotonic() if now is None else now
    if not pub.wants_cameras():
        return
    if now - state.get('last', 0.0) < PREVIEW_INTERVAL:
        return
    state['last'] = now
    # While a capture owns the exclusive STREAM_H264 subscriptions it publishes
    # viewer frames itself; sampling here too would double the SNAPSHOT load for
    # no benefit. SNAPSHOT itself stays available in every camd state.
    if not config.get('_hardware_fallback') and _capture_owns_cameras(camd_source, state, now):
        return

    def sample(device):
        try:
            _stamp, payload = camd_source.snapshot_jpeg(device, scale=PREVIEW_SCALE)
            pub.camera(device, payload)
            state.pop('error_' + device, None)
            state.pop('retry_' + device, None)
        except Exception as exc:
            # One unhealthy camera must not hide the others: back it off and
            # report each distinct failure once instead of every round.
            state['retry_' + device] = now + PREVIEW_FAILURE_BACKOFF
            message = str(exc)
            if state.get('error_' + device) != message:
                state['error_' + device] = message
                print('PREVIEW ' + device + ': ' + message, flush=True)

    workers = []
    for name in config.get('cameras', {}):
        device = 'cam_ros_' + name
        if now < state.get('retry_' + device, 0):
            continue
        thread = threading.Thread(target=sample, args=(device,),
                                  name='preview-' + device, daemon=True)
        thread.start()
        workers.append(thread)
    # Bound the round so a stuck camera cannot delay the next one.
    for thread in workers:
        thread.join(PREVIEW_FETCH_TIMEOUT)


def close_headband_previews(state):
    for source in state.pop('hardware',{}).values():
        try:source.close()
        except Exception:pass


def pump_headband_previews(config,pub,state,now=None):
    """Hardware decode/JPEG at source rate; SNAPSHOT is a bounded fallback."""
    now=time.monotonic() if now is None else now
    if not pub.wants_cameras():close_headband_previews(state);return
    from orangepi_collect.recorders.camera import camd_source
    from orangepi_collect.recorders.camera.h264_preview import HeadbandPreviewSource
    sources=state.setdefault('hardware',{})
    if not sources and _capture_owns_cameras(camd_source,state,now):return
    fallback={}
    for name,item in config.get('cameras',{}).items():
        device='cam_ros_'+name;source=sources.get(device)
        size=(1920,1200) if pub.wants_full_camera(device) else (640,400)
        if not pub.wants_camera(device):
            if source is not None:
                source.close();sources.pop(device,None)
            continue
        try:
            if source is not None and source.preview.size!=size:
                source.close();sources.pop(device,None);source=None
            if source is not None:
                source.health()
            elif now>=state.get('hw_retry_'+device,0):
                sources[device]=HeadbandPreviewSource(device,lambda payload,n=device:pub.camera(n,payload),*size)
                state.pop('hw_error_'+device,None)
            else:fallback[name]=item
        except Exception as error:
            if source is not None:
                try:source.close()
                except Exception:pass
                sources.pop(device,None)
            message=str(error)
            if state.get('hw_error_'+device)!=message:
                print('PREVIEW hardware '+device+': '+message,flush=True)
                state['hw_error_'+device]=message
            state['hw_retry_'+device]=now+PREVIEW_FAILURE_BACKOFF
            fallback[name]=item
    if fallback:
        cfg=dict(config,cameras=fallback,_hardware_fallback=True)
        # Existing healthy hardware previews run on independent threads while
        # a failed camera's snapshot times out or backs off.
        _pump_snapshot_previews(cfg,pub,state.setdefault('fallback',{}),now)


def main(config):
    from orangepi_collect.recorders.camera.uvc_source import UvcSource
    stop = threading.Event(); sources = []; threads = [];preview_state={}
    signal.signal(signal.SIGTERM, lambda *a: stop.set())
    signal.signal(signal.SIGINT, lambda *a: stop.set())
    pub = publisher()
    pub.bind_emg_sources(config.get('_preview_id'),
                        {slot:item['port'] for slot,item in config.get('emg',{}).items()})
    pub.bind_wrist_sources(config.get('_preview_id'),
                          {slot:item['device'] for slot,item in config.get('wrist_cameras',{}).items()})
    parent = os.getppid()

    def serial_reader(name, item):
        fd = None; original = None; exclusive = False
        try:
            if item.get('discovery', {}).get('present') is False: return
            fd = os.open(item['port'], os.O_RDWR|os.O_NOCTTY|os.O_NONBLOCK)
            fcntl.ioctl(fd, termios.TIOCEXCL); exclusive = True
            original = termios.tcgetattr(fd); attrs = copy.deepcopy(original)
            attrs[0] = attrs[1] = attrs[3] = 0
            attrs[2] = termios.CS8|termios.CREAD|termios.CLOCAL
            attrs[4] = attrs[5] = termios.B921600
            attrs[6][termios.VMIN] = attrs[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            buf = bytearray()
            while not stop.is_set():
                if not select.select([fd],[],[],.1)[0]: continue
                data = os.read(fd,8192)
                if not data: break
                # This owner supplies display envelopes only. The independent
                # recording owner retains every raw packet after handover.
                if not pub.wants_signal(name):buf.clear();continue
                buf.extend(data)
                for frame in preview_packets(buf):pub.emg(name,frame)
        except OSError as exc:
            print('PREVIEW '+name+': '+str(exc),flush=True)
        finally:
            if fd is not None:
                try:
                    if original is not None: termios.tcsetattr(fd,termios.TCSANOW,original)
                    if exclusive: fcntl.ioctl(fd,termios.TIOCNXCL)
                except OSError: pass
                os.close(fd)

    try:
        for name,item in config.get('wrist_cameras',{}).items():
            if item.get('discovery',{}).get('present') is False:
                write_wrist_report(name, wrist_camera_settings.unavailable_report(item, item.get(wrist_camera_settings.KEY)))
                continue
            try:
                source = UvcSource(item,lambda payload,*args,n=name: pub.camera(n,payload))
                sources.append(source)
                write_wrist_report(name, source.camera_settings)
            except Exception as exc:
                write_wrist_report(name, wrist_camera_settings.error_report(item, exc))
                print('PREVIEW '+name+': '+str(exc),flush=True)
        for name,item in config.get('emg',{}).items():
            t=threading.Thread(target=serial_reader,args=(name,item),daemon=True);t.start();threads.append(t)
        if config.get('eeg',{}).get('enabled') and config['eeg'].get('discovery',{}).get('present'):
            from eeg_bridge import EegProcess
            try:sources.append(EegProcess(config['eeg']))
            except Exception as exc:print('PREVIEW eeg: '+str(exc),flush=True)
        # Sleep only for the time left in the round. A fixed 50 ms pause used to
        # be added on top of each round, and because PREVIEW_INTERVAL is measured
        # from the previous round's start the two did not overlap: rounds of
        # 136 ms turned into a 209 ms period, about 4.7 fps instead of 8.
        while not stop.is_set():
            if os.getppid()!=parent: break
            started=time.monotonic()
            pump_headband_previews(config,pub,preview_state,now=started)
            # A hair past the interval, so pump's own interval gate never finds
            # the round a microsecond early and skips it.
            remaining=PREVIEW_INTERVAL+.002-(time.monotonic()-started)
            if stop.wait(max(.002,remaining)):break
    finally:
        stop.set()
        close_headband_previews(preview_state)
        for source in sources:
            try: source.close() if hasattr(source,'close') else source.unregister()
            except Exception: pass
        for t in threads: t.join(2)
        pub.close()


def run_preview(config):
    # Manual confirmation holds the capture lease while proving the shown EMG
    # sources. Ordinary idle preview keeps finished-session export available.
    if config.get('_exclusive_preview') is not True:return main(config)
    try:
        with wrist_camera_settings.capture_guard(project_paths.BASE):
            main(config)
    except ValueError as exc:
        print('PREVIEW unavailable: '+str(exc),flush=True)


if __name__=='__main__':run_preview(json.load(sys.stdin))
