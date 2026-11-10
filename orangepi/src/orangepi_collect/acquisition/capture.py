#!/usr/bin/env python3
"""Pi-only recorder. No project launcher, stim, pygame, or GUI imports."""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0,str(_Path(__file__).resolve().parents[1]/'common'))
import project_paths
import argparse
import csv
import fcntl
import json
import multiprocessing as mp
import os
from pathlib import Path
import queue
import select
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import traceback

import numpy as np
import gi
import imu_source
gi.require_version('Gst', '1.0')
from gi.repository import Gst

from project_paths import BASE, PROJECT, SRC, devices
from live_preview import publisher
import headband_calibration
from impedance_record import bind_start
HEADER = b'\xd2\xd2\xd2'
CONFIG=devices()
PORTS={name:config['port'] for name,config in CONFIG['emg'].items()}
CAMERAS=list(CONFIG['cameras'])
WRIST_CAMERAS=CONFIG.get('wrist_cameras',{})

def atomic_json(path, value):
    path=Path(path)
    tmp=path.with_suffix(path.suffix+'.tmp')
    with tmp.open('w',encoding='utf-8') as handle:
        json.dump(value,handle,ensure_ascii=False,indent=2,default=str)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)
    directory_fd=os.open(str(path.parent),os.O_RDONLY)
    try:os.fsync(directory_fd)
    finally:os.close(directory_fd)


def parse_frames(buf):
    frames = []
    while len(buf) >= 3:
        idx = buf.find(HEADER)
        if idx < 0:
            del buf[:-2]
            break
        if idx:
            del buf[:idx]
        if len(buf) < 29:
            break
        if buf[3] not in (0xaa, 0xbb):
            del buf[0]
            continue
        frames.append(bytes(buf[:29]))
        del buf[:29]
    return frames

def s24(payload):
    return [int.from_bytes(payload[i:i+3], 'big', signed=True) for i in range(0,24,3)]


def save_emg_chunks(path, jobs, errors, parent_pid):
    saved = 0
    try:
        while True:
            if os.getppid() != parent_pid:
                return
            try:
                rows = jobs.get(timeout=1)
            except queue.Empty:
                continue
            if rows is None:
                return
            emg = [r for r in rows if r[2][3] == 0xaa]
            imu = [r for r in rows if r[2][3] == 0xbb]
            dest = Path(path) / ('chunk_%06d.npz' % saved)
            temp = dest.with_suffix('.tmp')
            with temp.open('wb') as f:
                np.savez(f,
                    emg_timestamps=np.array([r[0]/1e9 for r in emg]),
                    emg_arrival_timestamps=np.array([r[0]/1e9 for r in emg]),
                    emg_monotonic_ns=np.array([r[1] for r in emg], dtype=np.int64),
                    emg_sn=np.array([r[2][4] for r in emg], dtype=np.uint8),
                    emg_data=np.array([s24(r[2][5:]) for r in emg], dtype=np.int32).reshape(-1,8),
                    imu_timestamps=np.array([r[0]/1e9 for r in imu]),
                    imu_arrival_timestamps=np.array([r[0]/1e9 for r in imu]),
                    imu_monotonic_ns=np.array([r[1] for r in imu], dtype=np.int64),
                    imu_sn=np.array([r[2][4] for r in imu], dtype=np.uint8),
                    imu_gyro=np.array([struct.unpack('>hhh',r[2][5:11]) for r in imu],dtype=np.float32).reshape(-1,3)*.0012,
                    imu_accel=np.array([struct.unpack('>hhh',r[2][11:17]) for r in imu],dtype=np.float32).reshape(-1,3)*.0005978,
                    raw_frames=np.array([list(r[2]) for r in rows],dtype=np.uint8),
                    raw_arrival_unix_ns=np.array([r[0] for r in rows],dtype=np.int64),
                    timestamp_basis=np.array('host_serial_read_arrival; not hardware sample timestamps'))
                f.flush()
                os.fsync(f.fileno())
            temp.replace(dest)
            saved += 1
    except BaseException as exc:
        errors.put(str(exc))
        raise

class EMG:
    def __init__(self, owner, name, port):
        self.owner, self.name, self.port = owner, name, port
        self.ready = False
        self.seen = 0
        self.last = time.monotonic()
        self.rows = []
        self.saved = self.emg_count = self.imu_count = self.dropped = 0
        self.prev_sn = None
        self.path = owner.path / name
        self.path.mkdir()
        ctx = mp.get_context('spawn')
        self.jobs = ctx.Queue(maxsize=8)
        self.save_errors = ctx.Queue()
        self.saver = ctx.Process(target=save_emg_chunks,args=(str(self.path),self.jobs,self.save_errors,os.getpid()))
        self.fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            fcntl.ioctl(self.fd, 0x540C)  # TIOCEXCL: prohibit another serial opener
            a = termios.tcgetattr(self.fd)
            a[0] = a[1] = a[3] = 0
            a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
            a[4] = a[5] = termios.B921600
            a[6][termios.VMIN] = a[6][termios.VTIME] = 0
            termios.tcsetattr(self.fd, termios.TCSANOW, a)
            termios.tcflush(self.fd, termios.TCIFLUSH)
        except BaseException:
            os.close(self.fd)
            raise
        self.saver.start()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        buf = bytearray()
        last_epoch = None
        last_flush = time.monotonic()
        try:
            while not self.owner.closing.is_set():
                if self.saver.exitcode is not None:
                    raise RuntimeError('EMG save process exited')
                if not select.select([self.fd], [], [], .1)[0]:
                    continue
                data = os.read(self.fd, 65536)
                if not data:
                    raise IOError('serial device disconnected')
                mono, wall = time.monotonic_ns(), time.time_ns()
                epoch = self.owner.start_ns
                if epoch != last_epoch:
                    buf.clear()
                    self.prev_sn = None
                    last_epoch = epoch
                buf.extend(data)
                frames = parse_frames(buf)
                self.seen += len(frames)
                for frame in frames:
                    publisher().emg(self.name, frame)
                    if frame[3] == 0xaa:
                        self.ready = True
                        self.last = time.monotonic()
                    with self.owner.gate:
                        if not self.owner.active or mono < self.owner.start_ns:
                            continue
                        sn = frame[4]
                        if self.prev_sn is not None:
                            gap = (sn - self.prev_sn - 1) & 255
                            self.dropped += gap
                            if gap:
                                self.owner.warn(self.name + ': serial sequence gap=' + str(gap))
                        self.prev_sn = sn
                        self.rows.append((wall, mono, frame))
                        if frame[3] == 0xaa:
                            self.emg_count += 1
                        else:
                            self.imu_count += 1
                if time.monotonic() - last_flush >= 2:
                    self.flush()
                    last_flush = time.monotonic()
        except Exception as exc:
            self.owner.fail(self.name + ': ' + str(exc))
        finally:
            try:
                self.flush()
            except Exception as exc:
                self.owner.fail(self.name + ' save: ' + str(exc))
            os.close(self.fd)
            try:
                self.jobs.put(None, timeout=3)
                self.saver.join(15)
                if self.saver.is_alive():
                    self.saver.terminate()
                    self.saver.join(3)
                    raise RuntimeError('EMG save process stalled')
                if self.saver.exitcode != 0:
                    raise RuntimeError('EMG save process failed')
            except Exception as exc:
                self.owner.fail(self.name + ': ' + str(exc))

    def flush(self):
        if not self.rows:
            return
        rows, self.rows = self.rows, []
        self.jobs.put_nowait(rows)
        self.saved += 1

from orangepi_collect.recorders.camera.mpp_camera_recorder import Camera, CameraDataError


def close_camera_recorders(cameras, started, error_list, summary, shutdown_complete):
    """Keep data-quality failures separate from unconfirmed camera shutdown."""
    for camera in cameras:
        try:
            summary[camera.name] = camera.close()
        except CameraDataError as exc:
            if started:
                error_list.append(camera.name + ': ' + str(exc))
        except Exception as exc:
            shutdown_complete = False
            if started:
                error_list.append(camera.name + ': ' + str(exc))
    return shutdown_complete

class BoardImu:
    """Record the Pi sensor_msgs/Imu source, independently of armband IMUs."""
    def __init__(self, owner):
        self.owner = owner
        self.name = 'board_imu'
        self.ready = False
        self.seen = 0
        self.last = time.monotonic()
        self.count = 0
        self.accepted = 0
        self.previous = None
        self.timestamp_duplicates = 0
        self.timestamp_backsteps = 0
        self.timestamp_anomaly_events = []
        self.jobs = queue.Queue(maxsize=1000)
        self.finishing = threading.Event()
        self.write_error = None
        self.queue_peak = 0
        self.max_write_seconds = 0.0
        self.path = owner.path / self.name
        self.path.mkdir()
        self.file = (self.path/'raw.csv').open('w',newline='')
        self.csv = csv.writer(self.file)
        self.csv.writerow(['ros_stamp_ns','arrival_unix_ns','arrival_monotonic_ns','sequence']+
            ['orientation_'+a for a in 'xyzw']+
            ['angular_velocity_'+a for a in 'xyz']+
            ['linear_acceleration_'+a for a in 'xyz']+
            ['orientation_covariance_'+str(i) for i in range(9)]+
            ['angular_velocity_covariance_'+str(i) for i in range(9)]+
            ['linear_acceleration_covariance_'+str(i) for i in range(9)])
        try:
            # EgoRecord v0.1.10 retired seeker_imu and its /imu_data_raw topic;
            # the ICM20602 is read straight from its character device instead.
            # Column names keep the sensor_msgs/Imu semantics the QC and the
            # LeRobot exporter expect; orientation stays zero because this
            # hardware does not output a fused quaternion.
            self.source = imu_source.Icm20602Source(self.callback)
        except BaseException:
            self.file.close()
            raise
        self.writer = threading.Thread(target=self.write_rows, daemon=True)
        self.writer.start()
    def callback(self,stamp,sequence,gyro,accel):
        self.seen += 1
        wall,mono=time.time_ns(),time.monotonic_ns()
        self.ready=True
        self.last=time.monotonic()
        with self.owner.gate:
            if self.finishing.is_set() or not self.owner.active or mono<self.owner.start_ns or stamp<self.owner.start_wall_ns:
                return
            if self.previous is not None and stamp <= self.previous:
                # Match original IMU TimestampSanity: retain exact samples and
                # report WARN; the original offline checker makes the verdict.
                kind = 'duplicate' if stamp == self.previous else 'backstep'
                if kind == 'duplicate':self.timestamp_duplicates += 1
                else:self.timestamp_backsteps += 1
                if len(self.timestamp_anomaly_events) < 8:
                    event = dict(kind=kind, previous_ns=self.previous,
                                 current_ns=stamp, sequence=sequence,
                                 arrival_unix_ns=wall)
                    self.timestamp_anomaly_events.append(event)
                    self.owner.warn('board_imu: QC_WARN source timestamp %s: previous=%d current=%d sequence=%d; original IMU rule retains sample' % (kind,self.previous,stamp,sequence))
            try:
                row = ([stamp,wall,mono,sequence]+
                    [0.0,0.0,0.0,0.0]+
                    list(gyro)+list(accel)+
                    [0.0]*27)
                # Only validate and enqueue under the shared START/STOP gate.
                # Disk IO belongs to the independent writer, never this gate.
                self.jobs.put_nowait(row)
                self.accepted+=1
                self.previous=stamp
                self.queue_peak=max(self.queue_peak,self.jobs.qsize())
            except queue.Full:
                self.owner.fail('board_imu: writer queue overflow')
            except Exception as exc:
                self.owner.fail('board_imu: '+str(exc))

    def write_rows(self):
        try:
            while True:
                try:
                    row=self.jobs.get(timeout=.1)
                except queue.Empty:
                    if self.finishing.is_set():break
                    continue
                started=time.monotonic()
                self.csv.writerow(row)
                self.count+=1
                if self.count%500==0:self.file.flush()
                self.max_write_seconds=max(self.max_write_seconds,time.monotonic()-started)
        except Exception as exc:
            self.write_error=str(exc)
            self.finishing.set()
            self.owner.fail('board_imu writer: '+str(exc))

    def close(self):
        self.source.close()
        with self.owner.gate:self.finishing.set()
        self.writer.join(15)
        if self.writer.is_alive():
            # Do not close a file still being used by a live writer thread.
            raise RuntimeError('board_imu writer did not drain within 15 seconds')
        try:
            self.file.flush()
            os.fsync(self.file.fileno())
        finally:
            self.file.close()
        if self.write_error:raise RuntimeError('board_imu writer: '+self.write_error)
        if self.count!=self.accepted:
            raise RuntimeError('board_imu accepted/written count mismatch: %d/%d' % (self.accepted,self.count))
        return dict(device=self.source.device,source=self.source.description,
                    frames=self.count,accepted_frames=self.accepted,
                    writer_queue_peak=self.queue_peak,writer_queue_capacity=self.jobs.maxsize,
                    max_write_seconds=self.max_write_seconds,
                    timestamp_basis='ICM20602 driver sampling instant (CLOCK_REALTIME domain)',
                    timestamp_qc_rule='original-framework-v1',
                    timestamp_duplicates=self.timestamp_duplicates,
                    timestamp_backsteps=self.timestamp_backsteps,
                    timestamp_anomaly_events=self.timestamp_anomaly_events,
                    orientation_basis='not reported by this hardware; stored as zero',
                    angular_velocity_units='rad/s',linear_acceleration_units='m/s^2')

    def health(self):
        self.source.health()
        if self.write_error:
            raise RuntimeError('board_imu writer: '+self.write_error)



class Capture:
    def __init__(self, path):
        self.path = path
        self.path.mkdir(parents=True,exist_ok=True)
        self.active = False
        self.start_ns = self.start_wall_ns = 0
        self.stop_ns = self.stop_wall_ns = 0
        self.gate = threading.RLock()
        self.closing = threading.Event()
        self.errors = queue.Queue()
        self.warnings = queue.Queue()
        self.warning_list = []
        self.cameras, self.emgs = [], []
        self.board = None
        self.eeg = None
        self.state = 'preparing'
        self.commands = queue.Queue()
        self.started = False
        self.screen_privacy_start = None
        self.error_list = []
        self.summary = {}
        self.headband_calibration_status = None

    def fail(self, message):
        self.stop_gate()
        self.errors.put(message)

    def warn(self,message):
        self.warnings.put(message)

    def status(self, state, **kwargs):
        self.state = state
        impedance = self.eeg.impedance_snapshot() if self.eeg else dict(required=False,supported=False,phase='not_selected',can_record=True)
        atomic_json(self.path/'status.json', dict(state=state,time=time.time(),pid=os.getpid(),recording_active=self.active,samples_seen=sum(d.seen for d in self.cameras+self.emgs+([self.board] if self.board else [])+([self.eeg] if self.eeg else [])),warning_count=len(self.warning_list),last_warning=self.warning_list[-1] if self.warning_list else None,headband_calibration=getattr(self,'headband_calibration_status',None),eeg_impedance=impedance,**kwargs))
        print(json.dumps(dict(state=state,**kwargs),ensure_ascii=False),flush=True)

    def stop_gate(self):
        # Close every device's acceptance gate before acknowledging STOP or errors.
        with self.gate:
            self.active=False
            if not self.stop_ns:
                self.stop_ns,self.stop_wall_ns=time.monotonic_ns(),time.time_ns()
                from screen_privacy import boot_identity,validate_screen_start
                proof=getattr(self,'screen_privacy_start',None)
                if proof is None:
                    try:proof=validate_screen_start(self.path)
                    except ValueError:pass
                atomic_json(self.path/'stop_gate.json',dict(stop_monotonic_ns=self.stop_ns,
                    stop_unix_ns=self.stop_wall_ns,recording_active=False,screen_privacy=proof,
                    worker_pid=os.getpid(),boot_id=boot_identity()))

    def request_stop(self):
        self.stop_gate()
        self.commands.put('STOP')

    def read_commands(self):
        for line in sys.stdin:
            if line.strip()=='STOP':self.request_stop()
            else:self.commands.put(line.strip())
        self.request_stop()

    def prepare_headband_calibration(self,camera_names,with_board):
        if camera_names or with_board:
            self.headband_calibration_status=headband_calibration.snapshot(self.path,CONFIG)
            if self.headband_calibration_status.get('conflict'):
                raise RuntimeError('本段已有不同的头环标定快照，请使用新的会话目录')

    def verify_headband_calibration(self):
        status=getattr(self,'headband_calibration_status',None)
        if status is not None:headband_calibration.verify_saved(self.path,status)

    def execute(self, camera_names, with_emg=True, with_board=True, emg_names=None, wrist_names=(), with_eeg=False):
        Gst.init(None)
        # No ROS node is needed any more: headband cameras come from camd and
        # the board IMU from /dev/icm20602.
        threading.Thread(target=self.read_commands,daemon=True).start()
        signal.signal(signal.SIGTERM, lambda *a: self.request_stop())
        signal.signal(signal.SIGINT, lambda *a: self.request_stop())
        self.status('preparing')
        try:
            self.prepare_headband_calibration(camera_names,with_board)
            self.status('preparing')
            if with_emg:
                for name,port in PORTS.items():
                    if emg_names is not None and name not in emg_names:continue
                    try:
                        self.emgs.append(EMG(self,name,port))
                    except Exception as exc:
                        raise RuntimeError(name + ': ' + str(exc)) from exc
            if with_board:
                self.board=BoardImu(self)
            if with_eeg:
                from eeg_bridge import EegProcess
                self.eeg=EegProcess(CONFIG['eeg'],output=self.path,owner=self)
            for name in camera_names:
                self.cameras.append(Camera(self,name))
            for name in wrist_names:
                self.cameras.append(Camera(self,name,usb=True))
            deadline = time.monotonic()+15
            last_heartbeat=0
            while True:
                if not self.errors.empty():
                    raise RuntimeError(self.errors.get())
                changed=False
                while not self.warnings.empty():
                    warning=self.warnings.get()
                    self.warning_list.append(warning)
                    with (self.path/'health_events.jsonl').open('a') as log:
                        log.write(json.dumps(dict(time=time.time(),warning=warning))+'\n')
                    changed=True
                if changed:self.status(self.state)
                now = time.monotonic()
                for camera in self.cameras:
                    camera.health()
                if self.eeg:self.eeg.health()
                # Surface a dead IMU reader thread directly instead of waiting
                # for the generic "no data for 3 seconds" timeout.
                if self.board:self.board.health()
                devices = self.cameras + self.emgs + ([self.board] if self.board else []) + ([self.eeg] if self.eeg else [])
                if self.state == 'preparing' and all(d.ready for d in devices):
                    self.status('ready')
                if self.state == 'preparing' and now > deadline:
                    raise RuntimeError('no first data: '+','.join(d.name for d in devices if not d.ready))
                if self.state in ('ready','recording'):
                    for d in devices:
                        if d is self.eeg and self.eeg.intentional_pause(now): continue
                        if now-d.last > 3:
                            raise RuntimeError(d.name+': no data for 3 seconds')
                if now-last_heartbeat>1 and self.state in ('ready','recording'):
                    self.status(self.state)
                    last_heartbeat=now
                command = None
                try:
                    command = self.commands.get(timeout=.05)
                except queue.Empty:
                    pass
                if command and command.startswith('{'):
                    request=json.loads(command)
                    if self.started or self.active or self.state != 'ready' or self.stop_ns:
                        raise RuntimeError('阻抗检查只允许在正式录制前执行')
                    if not self.eeg: raise RuntimeError('本段未选择脑电设备')
                    self.eeg.send_impedance(request)
                    self.status(self.state)
                    continue
                if command == 'START':
                    if self.state != 'ready' or self.stop_ns:
                        raise RuntimeError('START before all devices ready')
                    self.verify_headband_calibration()
                    if self.eeg and not self.eeg.can_record():
                        raise RuntimeError('START blocked: EEG impedance check/recovery is not confirmed')
                    from screen_privacy import validate_screen_start
                    with self.gate:
                        if self.state!='ready' or self.stop_ns:
                            raise RuntimeError('START cancelled: STOP was already acknowledged')
                        screen_proof=validate_screen_start(self.path)
                        self.start_wall_ns = time.time_ns()
                        self.start_ns = time.monotonic_ns()
                        start_gate=dict(start_monotonic_ns=self.start_ns,start_unix_ns=self.start_wall_ns,
                            screen_privacy=screen_proof)
                        if self.eeg:
                            start_gate['eeg_impedance']=bind_start(self.path,start_gate,
                                self.eeg.impedance_snapshot(),CONFIG['eeg'])
                        atomic_json(self.path/'start_gate.json',start_gate)
                        self.screen_privacy_start=screen_proof
                        self.active = self.started = True
                    self.status('recording',start_unix_ns=self.start_wall_ns,start_monotonic_ns=self.start_ns)
                if command == 'STOP':
                    break
                if self.started and (now-self.start_ns/1e9) % 5 < .08:
                    if os.statvfs(self.path).f_bavail*os.statvfs(self.path).f_frsize < 512*1024**2:
                        raise RuntimeError('disk free space below 512 MB')
        except Exception as exc:
            self.stop_gate()
            self.error_list.append(str(exc))
            self.status('error',errors=self.error_list)
            traceback.print_exc()
        finally:
            self.stop_gate()
            stop_ns, stop_wall = self.stop_ns, self.stop_wall_ns
            self.closing.set()
            shutdown_complete=True
            if not self.error_list:
                self.status('finalizing')
            if self.eeg:
                try:
                    self.summary['eeg']=self.eeg.close()
                    if self.started and not self.summary['eeg']['samples']:self.error_list.append('eeg: no recorded data')
                except Exception as exc:
                    shutdown_complete=False;self.error_list.append('eeg: '+str(exc))
            for e in self.emgs:
                e.thread.join(20)
                if e.thread.is_alive():
                    shutdown_complete=False
                    self.error_list.append(e.name+': writer failed to stop')
                if e.saver.is_alive():
                    shutdown_complete=False
                    self.error_list.append(e.name+': saver process still running')
                self.summary[e.name] = dict(port=e.port,baud=921600,emg_frames=e.emg_count,imu_frames=e.imu_count,
                    sequence_gaps=e.dropped,chunks=e.saved,timestamp_basis='serial read arrival; not hardware synchronization')
                if self.started and e.emg_count == 0:
                    self.error_list.append(e.name+': empty data or serial sequence gaps='+str(e.dropped))
            if self.board:
                try:
                    self.summary['board_imu']=self.board.close()
                    if self.started and not self.board.count:
                        self.error_list.append('board_imu: no recorded data')
                except Exception as exc:
                    shutdown_complete=False
                    self.error_list.append('board_imu: '+str(exc))
            shutdown_complete=close_camera_recorders(
                self.cameras,self.started,self.error_list,self.summary,shutdown_complete)
            while not self.errors.empty():
                self.error_list.append(self.errors.get())
            while not self.warnings.empty():
                warning=self.warnings.get();self.warning_list.append(warning)
                with (self.path/'health_events.jsonl').open('a') as log:
                    log.write(json.dumps(dict(time=time.time(),warning=warning))+'\n')
            for name,details in self.summary.items():
                # The generic text-log scanner treats JSON "error": null as a
                # warning. Keep EEG and wrist structured summaries as JSON;
                # real device faults remain in capture_summary/status and the
                # original encoder/worker logs, which are still scanned.
                filename=(name+'_summary.json' if name in ('eeg','cam_left_wrist','cam_right_wrist')
                          else name+'.log')
                (self.path/name/filename).write_text(json.dumps(details,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            self.summary.update(start_unix_ns=self.start_wall_ns,start_monotonic_ns=self.start_ns,
                stop_unix_ns=stop_wall,stop_monotonic_ns=stop_ns,errors=self.error_list,warnings=self.warning_list,
                headband_calibration=getattr(self,'headband_calibration_status',None),
                shutdown_complete=shutdown_complete,
                status='error' if self.error_list else ('success' if self.started else 'cancelled'))
            atomic_json(self.path/'capture_summary.json', self.summary)
            self.status(self.summary['status'],errors=self.error_list)
        return 1 if self.error_list else 0

if __name__ == '__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--output',required=True)
    p.add_argument('--selection',type=Path)
    p.add_argument('--cameras',nargs='+',choices=CAMERAS,default=CAMERAS)
    p.add_argument('--without-emg',action='store_true')
    p.add_argument('--without-board-imu',action='store_true')
    args=p.parse_args()
    import storage_paths
    storage_paths.validate_output(Path(args.output))
    capture_lock=(BASE/'runtime'/'capture.lock').open('w')
    fcntl.flock(capture_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    selected=json.loads(args.selection.read_text(encoding='utf-8'))['selected'] if args.selection else list(PORTS)+['cam_ros_'+c for c in CAMERAS]+['board_imu']+list(WRIST_CAMERAS)
    allowed=set(PORTS)|{'cam_ros_'+c for c in CAMERAS}|{'board_imu'}|set(WRIST_CAMERAS)|({'eeg'} if CONFIG.get('eeg',{}).get('enabled') else set())
    if not selected or not set(selected)<=allowed:raise ValueError('Invalid or empty device selection')
    sys.exit(Capture(Path(args.output).resolve()).execute([c for c in args.cameras if 'cam_ros_'+c in selected],not args.without_emg,not args.without_board_imu and 'board_imu' in selected,[n for n in PORTS if n in selected],[n for n in WRIST_CAMERAS if n in selected],'eeg' in selected))

