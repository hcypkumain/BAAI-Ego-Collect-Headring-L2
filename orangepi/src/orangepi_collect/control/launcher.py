#!/usr/bin/env python3
"""A/B voice controller; no GUI and no dependency on the existing launcher."""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0,str(_Path(__file__).resolve().parents[1]/'common'))
import project_paths
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import struct
import subprocess
import sys
import time
import traceback
import yaml
import ledger
import storage_paths

from project_paths import BASE, PROJECT, SRC, devices
from device_discovery import fingerprint
import wrist_calibration
import emg_calibration

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


from cloud_speaker import Speaker, CloudVoiceError

class ConsoleKeyboard:
    """Debug terminal only: full keyboard, Ctrl+C, no evdev grab."""
    def read(self,timeout=.1):
        if not select.select([sys.stdin],[],[],timeout)[0]:return None
        line=sys.stdin.readline()
        if not line:return 'escape'
        return {'q':'escape','quit':'escape','exit':'escape'}.get(line.strip().lower(),line.strip().lower())
    def discard(self):pass
    def close(self):pass

from keyboard_input import Keyboard
from web_controls import WebControls
from emg_insertion_controls import EmgInsertionControls
from dashboard import impedance_allows_recording
from reference_controls import ReferenceControlsMixin,reference_cli_queue

def image_queue(selected=None):
    return reference_cli_queue(BASE,PROJECT,ledger,selected)

class Controller(ReferenceControlsMixin, EmgInsertionControls, WebControls):
    class OwnerExited(RuntimeError):
        """The current worker can no longer receive control commands."""

    def __init__(self,smoke=0,selected_image=None,session_dir=None,console=False):
        explicit_image=selected_image is not None
        self.console=console
        self.selected_devices=None
        self.attempted=set()
        self.last_help=[]
        self.session_dir=Path(session_dir).resolve() if session_dir else None
        selected_image=selected_image or devices().get('first_image')
        self.retry_requested=False
        self.voice=Speaker()
        self.voice.cancelled=lambda:getattr(self,"quit",False)
        self.smoke=smoke
        self.worker=None
        self.log=None
        self.keyboard=None
        self.path=None
        self.image=None
        self.state='idle'
        self.quit=False
        self.notified_error=False
        self.warned_data_gap=False
        if not smoke:ledger.migrate_legacy()
        self.init_references(selected_image,explicit_image=explicit_image)
        self.exit_code=0
        self.success_count=0
        self.retained_created_ids=set()
        self.usb_fingerprint=None
        self.last_usb_poll=0
        self.last_device_scan=0
        self.last_scan=[]
        self.wrist_wizard=None
        self.emg_wizard=None

    def state_write(self):
        self.ui_revision=getattr(self,'ui_revision',0)+1
        self.voice.realtime=self.state in ('recording','countdown')
        self.voice.silent=self.state=='recording' or self.quit
        atomic_json(BASE/'runtime'/'controller.json',dict(pid=os.getpid(),state=self.state,
            session=str(self.path) if self.path else None,image=str(self.image) if self.image else None,
            success_count=self.success_count,selected_image=self.selected_image,selected_devices=getattr(self,'selected_devices',None),
            wrist_calibration_phase=getattr(getattr(self,'wrist_wizard',None),'phase',None),
            emg_calibration_phase=getattr(getattr(self,'emg_wizard',None),'phase',None),time=time.time()))
        self.ui_publish()

    def worker_status(self):
        if not self.path:return {}
        path=self.path/'status.json'
        try:info=path.stat()
        except FileNotFoundError:self._worker_status_cache=None;return {}
        key=(str(path),info.st_dev,info.st_ino,info.st_mtime_ns,info.st_size)
        cached=getattr(self,'_worker_status_cache',None)
        if cached and cached[0]==key:return cached[1]
        value=json.loads(path.read_text());after=path.stat()
        if key==(str(path),after.st_dev,after.st_ino,after.st_mtime_ns,after.st_size):self._worker_status_cache=(key,value)
        return value

    def device_label(self,name,fallback):
        config=getattr(self,'session_devices',None) or devices()
        if name=='eeg':return config.get('eeg',{}).get('label',fallback)
        for section in ('emg','wrist_cameras'):
            if name in config.get(section,{}):return config[section][name]['label']
        return fallback

    def error_voice(self, errors):
        text=' '.join(errors)
        labels=[]
        for key,label in [('emg_left','左臂肌电'),('emg_right','右臂肌电'),
                          ('cam_ros_bleft','后左相机'),('cam_ros_bright','后右相机'),
                          ('cam_left_wrist','左腕相机'),('cam_right_wrist','右腕相机'),
                          ('cam_ros_left','左相机'),('cam_ros_right','右相机'),('board_imu','板载惯性传感器')]:
            if key in text:
                labels.append(self.device_label(key,label))
        reason='采集出错'
        if 'no data' in text or 'no first data' in text:
            reason='数据中断，请检查设备连接'
        elif 'timestamp' in text:
            reason='设备源时间戳重复或倒退，请检查驱动和时间同步'
        elif 'disk' in text:
            reason='磁盘空间不足'
        elif 'serial' in text or 'sequence' in text:
            reason='肌电串口数据异常'
        elif 'encoder' in text or 'pipeline' in text or 'MP4' in text:
            reason='相机硬件编解码或视频保存异常'
        elif 'QC' in text:
            reason='数据检验发现错误，请查看检验报告'
        elif 'queue' in text:
            reason='相机写入队列已满'
        self.voice.say('错误。'+('，'.join(labels)+'，' if labels else '')+reason+'。本条数据未标记完成，错误详情已保存。')
        self.troubleshooting(text)

    def troubleshooting(self, text):
        import importlib.util,re
        spec=importlib.util.spec_from_file_location('pi_troubleshooting',PROJECT/'src/embodied_brain_collect/session/troubleshooting.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        categories=[]
        for token in re.findall(r'[a-zA-Z][a-zA-Z0-9_]*',text):
            category=module.slot_category(token)
            if category and category not in categories:categories.append(category)
        if 'serial' in text.lower() and 'emg' not in categories:categories.append('emg')
        messages=[('脑电数据已中断。请确认脑电帽电源和 USB 连接，再点击重新准备本段。'
                   if category=='eeg' else module.SLOT_GUIDES[category]) for category in categories]
        if 'cam_ros' in text:messages.append('板载相机请同时检查 ROS 相机驱动服务。')
        if 'board_imu' in text:messages.append('请检查板载惯性传感器驱动服务是否持续发布数据。')
        if not messages:messages=['请查看当前会话日志中的错误详情，排查后点击重新准备。']
        previous=getattr(self,'last_help',[])
        fresh=[message for message in messages if message not in previous]
        self.last_help=list(dict.fromkeys(previous+messages))
        for message in fresh:self.voice.say(message)

    def qc_voice(self):
        report=self.path/'qc_report.json'
        if not report.exists():return
        data=json.loads(report.read_text())
        for finding in data.get('findings',[]):
            if finding.get('level') in ('WARN','ERROR'):
                self.last_help.append(finding['message'])
                self.voice.say(finding['message']);self.troubleshooting(finding.get('subject','')+' '+finding['message'])
        for name,stream in data.get('streams',{}).items():
            findings=[f for f in stream.get('findings',[]) if f.get('level') in ('WARN','ERROR')]
            if findings:
                label={'emg_left':'左臂肌电','emg_right':'右臂肌电','board_imu':'板载惯性传感器',
                       'cam_left_wrist':'左腕相机','cam_right_wrist':'右腕相机',
                       'cam_ros_left':'前左相机','cam_ros_right':'前右相机',
                       'cam_ros_bleft':'后左相机','cam_ros_bright':'后右相机'}.get(name,name)
                detail=self.device_label(name,label)+'。'+'。'.join(f['message'] for f in findings)
                self.last_help.append(detail)
                self.voice.say(detail)
                self.troubleshooting(name)

    def announce(self):
        self.voice.say('本段任务参考图像，'+self.reference_image_title()+'。目前尚未录制，请按图像准备场景。设备就绪后，点击开始采集。')

    def prepare(self):
        if (BASE/'runtime/recording_reset_in_progress.json').exists():
            self.last_help=['旧采集数据正在归档，请等待维护完成后再准备。'];self.state_write();return False
        if self.worker is not None:
            self.last_help=['当前本段仍由采集进程持有，请结束或取消准备后再重试。']
            return False
        self.stop_preview()
        try:
            self.reference_refresh()
            if self.reference_error:raise ValueError(self.reference_error)
        except (OSError,ValueError,RecursionError) as error:
            self.reference_prepare_failed(error);return False
        if getattr(self,'usb_fingerprint',None) is not None and not self.worker:
            if fingerprint(devices().get('auto_discovery',{})) != self.usb_fingerprint:
                self.scan_devices()
                return
        if self.needs_wrist_calibration():
            self.start_wrist_calibration()
            return
        if self.needs_emg_calibration():
            self.start_emg_calibration()
            return
        if not self.images:
            self.state='exhausted';self.state_write()
            self.voice.say('当前所有参考图像已采集完成。点击准备本段，重新检查待采集图片。')
            return
        try:
            root=storage_paths.diagnostics_root() if self.smoke else (self.session_dir or storage_paths.session_root())
            storage_paths.validate_output(root)
        except storage_paths.StorageError as exc:
            self.state='failed_wait';self.state_write();self.last_help=[str(exc)]
            self.voice.say(str(exc)+'。处理后点击重新准备。')
            return
        # Validate the exact resolved configuration that will be frozen below.
        resolved=devices()
        if (set(self.selected_devices or ()).intersection(emg_calibration.SLOTS)
                and resolved.get('emg_calibration',{}).get('enabled',False)
                and not resolved.get('emg_calibration_status',{}).get('valid',False)):
            self.start_emg_calibration();return
        if (set(self.selected_devices or ()).intersection(wrist_calibration.SLOTS)
                and resolved.get('wrist_calibration',{}).get('enabled',False)
                and not resolved.get('wrist_calibration_status',{}).get('valid',False)):
            self.start_wrist_calibration();return
        self.keep_requested=False;self.owner_command_error=None;self.discard_requested=False
        new_path=None
        try:
            self.path=None
            selection=self.reference_begin_preparation()
            self.attempted.add(str(self.image))
            self.path,identity=self.reference_reserve_directory(root);new_path=self.path
            # Freeze resolved devices and the exact task/queue identity for this owner.
            self.session_devices=resolved
            atomic_json(self.path/'device_selection.json',dict(selected=list(self.selected_devices),device_config=resolved))
            metadata=dict(identity,reference_image=selection['asset']['relative_path'],
                reference_sha256=selection['asset']['sha256'],reference_selection=selection,
                test=bool(self.smoke),stim=False,pygame=False,
                device_config=resolved,selected_devices=list(self.selected_devices),storage_config=storage_paths.config(),
                keyboard_flow='Touch workflow: prepare and view reference without time limit, impedance, manual camera check, return to preparation, separate formal start, 5-to-1 countdown, hold 3s to stop, save or retry same task',
                timestamp_note='Shared host start gate; camera ROS source stamps and EMG host arrival stamps retained. No hardware synchronization is claimed.')
            self.reference_set_raw_metadata(metadata)
            atomic_json(self.path/'meta.json',metadata)
            atomic_json(self.path/'reference_queue.json',self._reference_active['entry']['snapshot'])
            self.log=(self.path/'worker.log').open('w')
            self.worker=subprocess.Popen([str(BASE/'.venvs/framework/bin/python'),'-u',str(SRC/'framework/framework_recorder.py'),'--output',str(self.path),'--wait-for-save'],
                stdin=subprocess.PIPE,stdout=self.log,stderr=subprocess.STDOUT,text=True,start_new_session=True)
        except Exception as error:
            if new_path is None:new_path=getattr(error,'path',None)
            if self.log is not None:
                try:self.log.close()
                except OSError:pass
                self.log=None
            if new_path is not None:
                try:atomic_json(new_path/'preparation_error.json',dict(self.reference_session_metadata(),error=str(error),time=time.time(),recording_started=False))
                except (OSError,ValueError):pass
            self.reference_prepare_failed(error,new_path);return False
        self.notified_error=False
        self.warned_data_gap=False
        self.last_help=[]
        self.state='preparing'
        self.state_write()
        self.announce()

    def command(self,text):
        if not self.worker or self.worker.poll() is not None:
            raise self.OwnerExited('采集子进程已退出')
        try:
            self.worker.stdin.write(text+'\n')
            self.worker.stdin.flush()
        except BrokenPipeError as exc:
            raise self.OwnerExited('采集子进程控制通道已关闭') from exc

    def owner_command_failed(self,error):
        # Keep this worker and its files until poll observes the actual exit.
        # A success summary cannot approve an unconfirmed STOP or start a retry.
        message=str(error)
        self.owner_command_error=message
        self.keep_requested=False
        self.retry_requested=False
        self.last_help=[message+'。本条未标记完成，请查看日志，等待采集进程退出后重新准备。']
        if self.path:
            atomic_json(self.path/'owner_control_error.json',dict(error=message,at=time.time()))
        self.state='error' if self.worker else 'failed_wait'
        self.state_write()
        self.poll()

    def begin(self):
        dashboard=getattr(self,'dashboard',None)
        privacy=getattr(dashboard,'privacy',None)
        if privacy is None:
            self.voice.say('请先打开触控采集界面，确认屏幕已连接。')
            return False
        if not self.worker or self.worker.poll() is not None or self.worker_status().get('state')!='ready':
            self.voice.say('设备尚未就绪，请先准备本段。')
            return False
        if 'eeg' in (self.selected_devices or ()) and not impedance_allows_recording(self.worker_status().get('eeg_impedance')):
            self.voice.say('请先结束阻抗检查、确认结果，并等待普通脑电采样恢复。')
            return False
        checks=getattr(dashboard,'camera_checks',None)
        if checks is not None and not checks.approved():
            self.voice.say('请先完成全部相机同屏检查，并确认头环和腕部相机方向。')
            return False
        privacy.arm(str(self.path.resolve()),worker_pid=self.worker_status().get('pid'),adapter_pid=self.worker.pid)
        try:
            deadline=time.monotonic()+12
            while not privacy.ready():
                self.ui_publish(force=True)
                if privacy.cancelled or self.quit:return False
                if self.worker.poll() is not None or self.worker_status().get('state')!='ready':
                    self.poll();return False
                if time.monotonic()>deadline:
                    self.voice.say('触控屏未确认黑屏，本次没有开始采集。请检查屏幕连接后重试。')
                    return False
                time.sleep(.025)
            return self._begin_after_privacy()
        finally:
            if not privacy.committed:
                self.countdown_status=None
                interrupt=getattr(self.voice,'interrupt',None)
                if callable(interrupt):interrupt()
                privacy.cancel_before_start()
                if self.state=='countdown':self.state='ready'
                self.state_write()
            elif self.state not in ('recording','confirm_stop','finalizing'):
                privacy.fault('开始状态未确认，保持隐私遮挡')
                self.ui_publish(force=True)

    def _begin_after_privacy(self):
        if 'eeg' in self.selected_devices and not impedance_allows_recording(self.worker_status().get('eeg_impedance')):
            self.voice.say('请先完成脑电阻抗检查，并确认检测已结束、设备配置已恢复。')
            return
        if self.worker_status().get('state')!='ready':
            self.voice.say('设备尚未就绪，请等待准备完成后再点击开始采集。')
            return
        if fingerprint(devices().get('auto_discovery',{})) != self.usb_fingerprint:
            self.release_prepared();self.scan_devices();return
        interrupt=getattr(self.voice,'interrupt',None)
        if callable(interrupt):interrupt()
        self.state='countdown'
        self.state_write()
        self.countdown_status={'remaining':5,'started_unix_ms':round(time.time()*1000),'deadline_unix_ms':round(time.time()*1000)+5000}
        for remaining,word in zip(range(5,0,-1),('五','四','三','二','一')):
            privacy=self.dashboard.privacy
            if privacy.cancelled or not privacy.ready():return False
            if self.quit:
                return
            if self.worker.poll() is not None or self.worker_status().get('state')!='ready':
                self.poll()
                return
            if 'eeg' in self.selected_devices and not impedance_allows_recording(self.worker_status().get('eeg_impedance')):
                self.state='ready';self.state_write()
                self.voice.say('脑电阻抗检查状态已变化，请检查后再开始。')
                return
            started=time.monotonic()
            self.countdown_status['remaining']=remaining
            self.countdown_status['tick_unix_ms']=round(time.time()*1000)
            enqueue=getattr(self,'countdown_voice',None)
            if callable(enqueue):enqueue(word)
            else:self.voice.say(word)
            tick=getattr(self.voice,'tick',None)
            if callable(tick):tick()
            publish=getattr(self,'ui_publish',None)
            if callable(publish):publish(force=True)
            time.sleep(max(0,1-(time.monotonic()-started)))
        self.countdown_status=None
        if self.keyboard:
            self.keyboard.discard()
        interrupt=getattr(self.voice,'interrupt',None)
        if callable(interrupt):interrupt()
        if 'eeg' in self.selected_devices and not impedance_allows_recording(self.worker_status().get('eeg_impedance')):
            self.state='ready';self.state_write();return
        # A cancellation or owner failure can arrive during the final countdown sleep.
        # Keep the impedance gate above, and recheck immediately before START.
        if self.quit:
            return
        if not self.worker or self.worker.poll() is not None or self.worker_status().get('state')!='ready':
            self.poll()
            return
        privacy=self.dashboard.privacy
        atomic_json(self.path/'screen_privacy_gate.json',privacy.commit())
        self.command('START')
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            status=self.worker_status()
            if status.get('state')=='recording':
                privacy.recording()
                self.recording_since=time.time()
                self.state='recording'
                self.state_write()
                print('RECORDING '+str(self.path),flush=True)
                if privacy.cancelled:return self.stop_before_prompt()
                return
            if status.get('state') in ('error','success') or self.worker.poll() is not None:
                self.poll()
                return
            time.sleep(.05)
        raise RuntimeError('开始采集确认超时')

    def finish(self):
        self.keep_requested=True
        if getattr(self,'worker',None) is not None and self.worker.poll() is None:
            self.command('KEEP')
        self.state='finalizing'
        self.finalize_deadline=time.monotonic()+1800
        self.state_write()
        self.voice.say('采集已停止，正在保存并检验，请等待。')

    def discard_and_retry(self):
        if self.state!='confirm_stop':raise ValueError('请先停止采集，再选择重采本段')
        privacy=getattr(getattr(self,'dashboard',None),'privacy',None)
        if privacy is not None and privacy.snapshot().get('required'):
            raise ValueError('停止状态尚未确认，请稍后重试')
        if not self.path or not getattr(self,'worker',None):raise ValueError('没有可重采的当前段')
        atomic_json(self.path/'discard_request.json',dict(schema_version=1,session_id=self.path.name,
            directory=str(self.path.resolve()),reason='operator_discard_and_retry',requested_at_unix=time.time()))
        self.keep_requested=False;self.retry_requested=True;self.discard_requested=True
        self.state='discarding';self.discard_deadline=time.monotonic()+60
        if self.worker.poll() is None:self.command('DISCARD')
        self.state_write()
        self.voice.say('本段已放弃，正在关闭设备并删除本段文件，随后重新准备同一任务。')
        self.poll()

    def poll_discard(self):
        if self.worker.poll() is None:
            if time.monotonic()<=self.discard_deadline:return
            # A wedged owner must release every device before any file removal.
            os.killpg(self.worker.pid,signal.SIGKILL);self.worker.wait(timeout=5)
        path=self.path
        self._reference_reap_outcome='discarded'
        self.reap()
        try:
            self.recording_store.discard_current(path)
        except (OSError,ValueError) as error:
            self.discard_requested=False;self.retry_requested=False
            self.state='failed_wait';self.last_help=['本段文件未能清理：'+str(error)+'。请处理存储问题后重试。']
            self.state_write();self.voice.say(self.last_help[0]);return
        self.discard_requested=False;self.retry_requested=False;self.path=None
        if getattr(self,'last_review_path',None)==path:
            import recording_review
            self.last_review_path=recording_review.latest_recording(self.recording_store.roots()[0])
        self.reference_refresh(force=True)
        self.state='idle';self.exit_code=0;self.state_write()
        if not self.quit:self.prepare()

    def stop_before_prompt(self):
        privacy=getattr(getattr(self,'dashboard',None),'privacy',None)
        if privacy is not None:
            privacy.stopping()
        interrupt=getattr(self.voice,'interrupt',None)
        if callable(interrupt):interrupt()
        try:self.command('STOP')
        except self.OwnerExited as exc:
            self.owner_command_failed(exc)
            return False
        deadline=time.monotonic()+3
        while not (self.path/'stop_gate.json').exists():
            if self.worker.poll() is not None:
                self.owner_command_failed(self.OwnerExited('采集子进程在停止确认前退出'))
                return False
            if time.monotonic()>deadline:raise RuntimeError('停止采集确认超时')
            time.sleep(.01)
        if privacy is not None:
            proof=json.loads((self.path/'stop_gate.json').read_text())
            if not privacy.confirm_stopped(proof):
                raise RuntimeError('停止状态未确认，保持隐私遮挡')
        self.state='confirm_stop'
        self.state_write()
        self.voice.say('采集已停止。请点击确认保存，或选择重采本段。')
        return True

    def scan_devices(self,announce=True,quiet=False):
        self.stop_preview()
        previous_state=self.state
        scanned_fingerprint=fingerprint(devices().get('auto_discovery',{}))
        self.state='scanning';self.state_write()
        try:
            result=subprocess.run([str(BASE/'.venvs/ros/bin/python'),str(SRC/'acquisition/scan_devices.py')],
                capture_output=True,text=True,timeout=15)
            if result.returncode:raise ValueError('设备扫描失败，请查看驱动服务')
            scan=json.loads(result.stdout)
            if not isinstance(scan,list) or any(not isinstance(item,dict)
                    or not isinstance(item.get('name'),str) or not item['name']
                    or not isinstance(item.get('label'),str)
                    or type(item.get('connected')) is not bool for item in scan):
                raise ValueError('设备扫描结果格式无效')
        except (OSError,ValueError,subprocess.TimeoutExpired) as exc:
            reason=('设备扫描超时' if isinstance(exc,subprocess.TimeoutExpired)
                else '设备扫描结果不是有效 JSON' if isinstance(exc,json.JSONDecodeError) else str(exc))
            print('DEVICE SCAN failed '+repr(exc),flush=True)
            message='设备扫描未完成：'+reason+'。请检查设备后重新扫描。'
            self.last_help=[message]
            self.scan_ok=False;self.scan_available=[];self.selected_devices=None
            # Retain identities for diagnosis, without presenting old readiness
            # as current evidence or permitting a stale device confirmation.
            self.last_scan=[dict(item,connected=False,status='scan_failed',error=message)
                for item in getattr(self,'last_scan',[])]
            self.last_device_scan=time.monotonic()
            self.usb_fingerprint=scanned_fingerprint
            atomic_json(BASE/'runtime/devices.json',self.last_scan)
            self.state='device_confirm';self.state_write()
            if not quiet:self.voice.say(message)
            return False
        # Display labels and enumeration order are not a hardware change.
        old_signature=sorted((x['name'],x['connected'],x.get('present'),x.get('device')) for x in getattr(self,'last_scan',[]))
        new_signature=sorted((x['name'],x['connected'],x.get('present'),x.get('device')) for x in scan)
        changed=old_signature!=new_signature
        print('DEVICE SCAN '+json.dumps({'announce':announce,'changed':changed,'previous_state':previous_state,
            'before':old_signature,'after':new_signature},ensure_ascii=False),flush=True)
        self.last_scan=scan
        self.last_help=[]
        self.web_config=devices()
        self.last_device_scan=time.monotonic()
        # A plug/unplug during the scan must trigger another scan, not be lost.
        self.usb_fingerprint=scanned_fingerprint
        atomic_json(BASE/'runtime/devices.json',scan)
        self.scan_available=[item['name'] for item in scan if item['connected']]
        self.scan_ok=bool(self.scan_available)
        connected=[item['label'] for item in scan if item['connected']]
        missing=[item['label'] for item in scan if not item['connected']]
        self.state=previous_state if not announce and not changed else 'device_confirm'
        if changed:self.selected_devices=None
        self.state_write()
        if quiet:return True
        if not announce and not changed:return True
        self.voice.say('Orange Pi 采集程序。当前接入的数据采集设备：'+('，'.join(connected) or '尚未发现采集设备')+'。')
        if missing:self.voice.say('未收到有效数据：'+'，'.join(missing)+'。请检查连接。')
        if any(x.get('status')=='no_data' and x['name'].startswith('emg') for x in scan):
            self.voice.say('肌电接收器已识别，但未收到有效肌电。请检查臂环电源和连接，程序会自动重试。')
        self.voice.say('请点击确认设备，或选择重新扫描。仅采集已接入并确认的设备。')
        return True

    def needs_wrist_calibration(self):
        if not set(getattr(self,'selected_devices',None) or ()).intersection(wrist_calibration.SLOTS):
            return False
        config=devices()
        return config.get('wrist_calibration',{}).get('enabled',False) and not config.get('wrist_calibration_status',{}).get('valid',False)

    def start_wrist_calibration(self):
        if getattr(self,'worker',None):
            return  # The capture worker has exclusive ownership until released.
        self.stop_preview()
        config=devices()
        if not config.get('wrist_calibration',{}).get('enabled',False):return
        if not set(wrist_calibration.SLOTS).issubset(getattr(self,'scan_available',[])):
            self.state='device_confirm';self.state_write()
            self.voice.say(wrist_calibration.PROMPTS['pair']);return
        wrist_calibration.invalidate(BASE)
        # Discard any old role ordering before capturing the temporary A/B sources.
        config=devices()
        self.refresh_wrist_labels(config)
        try:
            self.wrist_wizard=wrist_calibration.Wizard(config,wrist_calibration.boot_id(),wrist_calibration.current_stamp(config))
        except wrist_calibration.CalibrationError:
            self.wrist_wizard=None
            self.scan_devices();self.voice.say(wrist_calibration.PROMPTS['pair']);return
        phase='choose' if getattr(self,'dashboard',None) else 'baseline'
        self.state='wrist_'+phase;self.state_write()
        self.voice.say(wrist_calibration.PROMPTS[phase])

    def set_wrist_method(self,method):
        wizard=getattr(self,'wrist_wizard',None)
        if method not in ('manual','occlusion') or wizard is None or not self.state.startswith('wrist_') or self.worker:
            raise ValueError('当前不能选择腕部标定方式')
        if not wizard.unchanged(wrist_calibration.boot_id(),wrist_calibration.current_stamp(wizard.config)):
            self.calibration_connection_changed();return
        # A new session cancels drafts in every browser without reopening cameras.
        self.wrist_wizard=wrist_calibration.Wizard(wizard.config,wizard.boot,wizard.stamp)
        self.state='wrist_manual' if method=='manual' else 'wrist_baseline'
        self.state_write()
        self.voice.say(wrist_calibration.PROMPTS['manual' if method=='manual' else 'baseline'])

    def assign_wrist_roles(self,value):
        wizard=getattr(self,'wrist_wizard',None)
        preview=getattr(self,'preview_worker',None)
        if self.state!='wrist_manual' or wizard is None or self.worker:
            raise ValueError('当前不能确认腕部左右')
        if (not isinstance(value,dict) or value.get('session_id')!=wizard.session_id
                or value.get('preview_id')!=getattr(self,'preview_snapshot_id',None)
                or not value.get('preview_id') or preview is None or preview.poll() is not None):
            raise ValueError('画面或标定步骤已变化，请重新选择左右')
        if not wizard.unchanged(wrist_calibration.boot_id(),wrist_calibration.current_stamp(wizard.config)):
            self.calibration_connection_changed();raise ValueError('相机连接已变化，请重新确认设备')
        sources=dict(getattr(self,'preview_sources',{}))
        if set(sources.values())!=wizard.paths:
            raise ValueError('相机来源已变化，请重新选择左右')
        for slot in wrist_calibration.SLOTS:
            try:age=time.time()-(self.preview_dir/(slot+'.jpg')).stat().st_mtime
            except OSError:raise ValueError('请等待两路腕部实时画面后再确认')
            if not 0<=age<3:raise ValueError('腕部画面已过期，请等待实时画面后重新确认')
        try:proof=wrist_calibration.manual_proof(wizard.boot,wizard.stamp,sources,value.get('left_slot'))
        except wrist_calibration.CalibrationError:raise ValueError('左右分配无效，请重新选择')
        self.stop_preview()
        # Recheck after readers close: a replug during confirmation must not save.
        if not wizard.unchanged(wrist_calibration.boot_id(),wrist_calibration.current_stamp(wizard.config)):
            self.calibration_connection_changed();raise ValueError('相机连接已变化，请重新确认设备')
        self.finish_wrist_calibration(proof)

    def calibration_connection_changed(self):
        self.wrist_wizard=None
        self.selected_devices=None
        wrist_calibration.invalidate(BASE)
        self.voice.say(wrist_calibration.PROMPTS['changed'])
        self.scan_devices()

    def advance_wrist_calibration(self):
        self.stop_preview(clear=False)
        wizard=getattr(self,'wrist_wizard',None)
        if wizard is None or getattr(self,'worker',None):return
        if not wizard.unchanged(wrist_calibration.boot_id(),wrist_calibration.current_stamp(wizard.config)):
            self.calibration_connection_changed();return
        try:
            result=subprocess.run([str(BASE/'.venvs/ros/bin/python'),str(SRC/'acquisition/calibration_probe.py')],
                input=json.dumps(wizard.config),capture_output=True,text=True,timeout=12)
            data=json.loads(result.stdout)
            if data.get('error')=='devices_changed':
                self.calibration_connection_changed();return
            if result.returncode or not data.get('ok'):
                raise wrist_calibration.CalibrationError(data.get('error','probe_failed'))
            if not wizard.unchanged(data['boot_id'],data['stamp']) or not wizard.unchanged(wrist_calibration.boot_id(),wrist_calibration.current_stamp(wizard.config)):
                self.calibration_connection_changed();return
            wizard.accept(data['statistics'])
        except (wrist_calibration.CalibrationError,ValueError,KeyError,subprocess.TimeoutExpired) as exc:
            print('WRIST CALIBRATION retry '+type(exc).__name__,flush=True)
            self.voice.say(wrist_calibration.PROMPTS['retry'])
            self.voice.say(wrist_calibration.PROMPTS[wizard.phase]);return
        self.state='wrist_'+wizard.phase;self.state_write()
        if wizard.phase!='done':
            self.voice.say(wrist_calibration.PROMPTS[wizard.phase]);return
        self.finish_wrist_calibration(wizard.proof())

    def finish_wrist_calibration(self,proof):
        wrist_calibration.save(BASE,proof)
        selected=tuple(self.selected_devices or self.scan_available)
        self.wrist_wizard=None
        # The final measurement already proved both cameras are streaming and
        # unchanged. Reopening every device here flashes the UI back to step 1.
        config=devices()
        if not config.get('wrist_calibration_status',{}).get('valid',False):
            self.calibration_connection_changed();return
        self.refresh_wrist_labels(config)
        self.selected_devices=tuple(n for n in selected if n in self.scan_available)
        self.state='idle';self.state_write()
        if self.needs_emg_calibration():
            self.voice.say(emg_calibration.PROMPTS['after_wrist'])
            self.start_emg_calibration();return
        self.voice.say(wrist_calibration.PROMPTS['done'])

    def refresh_wrist_labels(self,config):
        self.web_config=config
        by_path={row.get('device'):row for row in getattr(self,'last_scan',[]) if row['name'] in wrist_calibration.SLOTS}
        updated=[]
        for row in getattr(self,'last_scan',[]):
            name=row['name']
            if name in wrist_calibration.SLOTS:
                item=config['wrist_cameras'][name]
                row=dict(by_path.get(item['device'],row),name=name,label=item['label'],
                         device=item['device'],config=item,
                         role_confirmed=bool(config.get('wrist_calibration_status',{}).get('valid')))
            updated.append(row)
        self.last_scan=updated
        atomic_json(BASE/'runtime/devices.json',updated)

    def needs_emg_calibration(self):
        if not set(getattr(self,'selected_devices',None) or ()).intersection(emg_calibration.SLOTS):
            return False
        config=devices()
        return config.get('emg_calibration',{}).get('enabled',False) and not config.get('emg_calibration_status',{}).get('valid',False)

    def start_emg_manual_calibration(self):
        if getattr(self,'worker',None):return
        self.stop_preview()
        config=devices()
        if not config.get('emg_calibration',{}).get('enabled',False):return
        if not set(emg_calibration.SLOTS).issubset(getattr(self,'scan_available',[])):
            self.state='device_confirm';self.state_write()
            self.voice.say(emg_calibration.PROMPTS['pair']);return
        emg_calibration.invalidate(BASE)
        try:
            self.emg_wizard=emg_calibration.Wizard(config,emg_calibration.boot_id(),emg_calibration.current_stamp(config))
        except emg_calibration.CalibrationError:
            self.emg_wizard=None
            self.scan_devices();self.voice.say(emg_calibration.PROMPTS['pair']);return
        self.state='emg_manual';self.state_write()
        self.voice.say(emg_calibration.PROMPTS['manual'])

    def emg_connection_changed(self):
        self.emg_wizard=None;self.selected_devices=None
        emg_calibration.invalidate(BASE)
        self.voice.say(emg_calibration.PROMPTS['changed']);self.scan_devices()

    def assign_emg_roles(self,value):
        wizard=getattr(self,'emg_wizard',None)
        preview=getattr(self,'preview_worker',None)
        if self.state!='emg_manual' or wizard is None or self.worker:
            raise ValueError('当前不能确认肌电左右')
        if (not isinstance(value,dict) or set(value)!={'session_id','preview_id','left_slot'}
                or value.get('session_id')!=wizard.session_id
                or value.get('preview_id')!=getattr(self,'preview_snapshot_id',None)
                or not value.get('preview_id') or preview is None or preview.poll() is not None):
            raise ValueError('预览或确认步骤已变化，请重新选择左右')
        if not wizard.unchanged(emg_calibration.boot_id(),emg_calibration.current_stamp(wizard.config)):
            self.emg_connection_changed();raise ValueError('肌电连接已变化，请重新确认设备')
        sources=dict(getattr(self,'emg_preview_sources',{}))
        if set(sources)!=set(emg_calibration.SLOTS) or set(sources.values())!=wizard.paths:
            raise ValueError('肌电来源已变化，请重新选择左右')
        evidence={}
        for slot in emg_calibration.SLOTS:
            try:
                signal=json.loads((self.preview_dir/(slot+'.json')).read_text())
                age=time.time()-signal['time']
                fresh=(0<=age<3 and signal.get('preview_id')==value['preview_id']
                    and signal.get('source_path')==sources[slot]
                    and type(signal.get('count')) is int and signal['count']>0
                    and isinstance(signal.get('rows'),list) and bool(signal['rows']))
            except (OSError,ValueError,TypeError,KeyError):fresh=False
            if not fresh:raise ValueError('请等待两路当前实时肌电信号后重新选择左右')
            evidence[slot]=dict(source_path=sources[slot],time=signal['time'],count=signal['count'])
        try:proof=emg_calibration.manual_proof(wizard.boot,wizard.stamp,sources,value.get('left_slot'))
        except emg_calibration.CalibrationError:raise ValueError('肌电左右分配无效，请重新选择')
        proof['preview']=dict(session_id=wizard.session_id,preview_id=value['preview_id'],signals=evidence)
        self.stop_preview()
        # Closing readers releases the capture lease; replugging must invalidate the draft.
        if not wizard.unchanged(emg_calibration.boot_id(),emg_calibration.current_stamp(wizard.config)):
            self.emg_connection_changed();raise ValueError('肌电连接已变化，请重新确认设备')
        self.finish_emg_calibration(proof)

    def finish_emg_calibration(self,proof):
        emg_calibration.save(BASE,proof)
        selected=tuple(self.selected_devices or self.scan_available)
        self.emg_wizard=None
        config=devices()
        if not config.get('emg_calibration_status',{}).get('valid',False):
            self.emg_connection_changed();return
        # Reuse readiness by physical source while changing only its body-side label.
        by_path={row.get('device'):row for row in getattr(self,'last_scan',[])
                 if row['name'] in emg_calibration.SLOTS}
        updated=[]
        for row in getattr(self,'last_scan',[]):
            name=row['name']
            if name in emg_calibration.SLOTS:
                item=config['emg'][name]
                row=dict(by_path.get(item['port'],row),name=name,label=item['label'],
                         device=item['port'],config=item,role_confirmed=True)
            updated.append(row)
        self.last_scan=updated;self.web_config=config
        atomic_json(BASE/'runtime/devices.json',updated)
        self.selected_devices=tuple(n for n in selected if n in self.scan_available)
        self.state='idle';self.state_write()
        self.voice.say(emg_calibration.PROMPTS['done'])

    def poll_hotplug(self):
        if self.state == 'emg_pairing':
            self.poll_emg_pairing()
            return
        if self.worker and self.state=='ready' and time.monotonic()-getattr(self,'last_usb_poll',0)>2:
            self.last_usb_poll=time.monotonic()
            if fingerprint(devices().get('auto_discovery',{}))!=self.usb_fingerprint:
                self.release_prepared();self.scan_devices()
            return
        emg=getattr(self,'emg_wizard',None)
        if emg is not None and not self.worker:
            now=time.monotonic()
            if now-getattr(self,'last_usb_poll',0)<2:return
            self.last_usb_poll=now
            if not emg.unchanged(emg_calibration.boot_id(),emg_calibration.current_stamp(emg.config)):
                self.emg_connection_changed()
            return
        wizard=getattr(self,'wrist_wizard',None)
        if wizard is not None and not self.worker:
            now=time.monotonic()
            if now-getattr(self,'last_usb_poll',0)<2:return
            self.last_usb_poll=now
            if not wizard.unchanged(wrist_calibration.boot_id(),wrist_calibration.current_stamp(wizard.config)):
                self.calibration_connection_changed()
            return
        # Opening a second reader during a recording can steal EMG bytes.
        if self.worker or self.state not in ('device_confirm','idle','failed_wait','exhausted'):
            return
        now=time.monotonic()
        if now-getattr(self,'last_usb_poll',0)<getattr(self,'usb_poll_seconds',2):return
        settings=devices().get('auto_discovery',{})
        self.usb_poll_seconds=max(1,float(settings.get('poll_seconds',2)))
        self.last_usb_poll=now
        if not settings.get('enabled',False):return
        current=fingerprint(settings)
        changed=current!=getattr(self,'usb_fingerprint',None)
        pending=any(x.get('present') and not x['connected'] for x in getattr(self,'last_scan',[]))
        retry=pending and now-getattr(self,'last_device_scan',0)>=settings.get('retry_no_data_seconds',15)
        if changed or retry:
            self.scan_devices(announce=False)
            if self.keyboard:self.keyboard.discard()

    def reap(self):
        self.worker.wait(timeout=2)
        try:
            try:self.worker.stdin.close()
            except BrokenPipeError:pass  # A failed command can leave a buffered flush.
            self.log.close()
        finally:
            self.worker=None
            self.log=None
            self.reference_reaped()

    def poll(self):
        tick=getattr(self.voice,'tick',None)
        if callable(tick):tick()
        if not self.worker:
            return
        if getattr(self,'discard_requested',False):
            self.poll_discard();return
        if self.state=='finalizing' and time.monotonic()>getattr(self,'finalize_deadline',float('inf')) and self.worker.poll() is None:
            os.killpg(self.worker.pid,signal.SIGKILL)
            self.worker.wait(timeout=3)
            atomic_json(self.path/'controller_error.json',dict(error='finalization timed out',at=time.time()))
        if self.state=='confirm_stop':return  # QC may finish; operator must decide before ledger commit.
        status=self.worker_status()
        state=status.get('state')
        if self.state!='recording' and status.get('warning_count',0)>0 and not self.warned_data_gap:
            self.warned_data_gap=True
            self.voice.say('检测到数据不连续，异常已记录，请查看检验报告。')
        if state in ('error','qc_error') and not self.notified_error:
            self.notified_error=True
            self.state='error'
            self.state_write()
            self.error_voice(status.get('errors',[]))
        if self.state=='preparing' and state=='ready':
            self.state='ready'
            self.state_write()
            self.voice.say('设备已经就绪，目前尚未录制。请充分查看参考图，再手动检查相机。确认画面后返回准备页，点击开始正式采集才会倒计时。')
            print('READY: reference viewing, manual camera check, then explicit formal start',flush=True)
        if self.worker.poll() is not None:
            summary_path=self.path/'capture_summary.json'
            if not summary_path.exists():
                atomic_json(summary_path,dict(status='error',errors=['worker exited without final summary: exit='+str(self.worker.returncode)],time=time.time()))
            summary=json.loads(summary_path.read_text())
            owner_error=getattr(self,'owner_command_error',None)
            ok=summary.get('status')=='success' and self.worker.returncode==0 and not owner_error
            # Detailed findings remain in the report and web review. Do not
            # postpone ledger commit behind a long queue of spoken warnings.
            retry=getattr(self,'retry_requested',False)
            outcome='error' if not ok and summary.get('status')!='cancelled' else 'failed' if retry else 'success' if ok else 'interrupted'
            if owner_error:outcome='error'
            if self.quit and not getattr(self,'keep_requested',False):outcome='interrupted'
            meta_path=self.path/'meta.yaml'
            if meta_path.exists():
                meta=yaml.safe_load(meta_path.read_text(encoding='utf-8')) or {}
            else:
                source=self.path/'meta.json'
                raw=json.loads(source.read_text(encoding='utf-8')) if source.exists() else {}
                meta=yaml.safe_load((PROJECT/'configs/meta.yaml').read_text(encoding='utf-8')) or {}
                meta.update(session_dir=str(self.path),environment=raw.get('reference_image','').replace('configs/environments/','',1),pi=raw)
            meta.update(self.reference_session_metadata())
            meta['status']=outcome
            meta['retention_state']='kept' if outcome=='success' else outcome
            meta['retained_at_unix']=time.time() if outcome=='success' else None
            meta['reference_state']='collected' if outcome=='success' else 'uncollected'
            meta['upload_state']='retained_for_upload' if outcome=='success' else 'excluded'
            ledger.atomic_yaml(meta_path,meta)
            if retry:
                atomic_json(self.path/'retry.json',dict(status=outcome,reason='operator requested retry',reference_image=str(self.image),at=time.time()))
            if not self.smoke:ledger.mark(self.image,self.path,outcome)
            if outcome=='success':
                self.success_count+=1
                self.retained_created_ids=getattr(self,'retained_created_ids',set());self.retained_created_ids.add(self.path.name)
            elif not self.notified_error and not retry and summary.get('status')!='cancelled':
                self.error_voice(summary.get('errors',['采集进程异常退出，请检查日志']))
            self.exit_code=0 if (ok or summary.get('status')=='cancelled') and not owner_error else 1
            self._reference_reap_outcome=outcome
            self.reap();self.retry_requested=False
            self.last_review_path=self.path
            self.last_completed={'session':self.path.name,'outcome':outcome,'qc_level':summary.get('qc_level')}
            if self.quit:return
            if self.smoke:
                self.quit=True;self.state='idle';self.state_write();return
            if retry:
                self.voice.say('本条已标记失败并保留，重新准备当前图像。')
                self.prepare();return
            if not ok:
                self.state='failed_wait';self.state_write()
                self.voice.say('本条未标记已用。请点击重采本段，或重复播报错误排查提示。')
                return
            self.state='idle';self.state_write()
            hint='本段存在质量提示，请在网页查看详情。' if summary.get('warnings') or summary.get('qc_level')=='WARN' else ''
            self.voice.say('本段已保存并完成检验。'+hint+'可以回看录像，或点击准备本段开始下一段任务。')

    def key(self,key):
        if key in ('a','b','c','d','enter','escape'):
            interrupt=getattr(self.voice,'interrupt',None)
            if callable(interrupt):interrupt()
        if self.state == 'emg_pairing':
            if key == 'escape':self.quit = True
            elif key == 'b':self.start_emg_calibration()
            if self.keyboard:self.keyboard.discard()
            return
        if self.state.startswith('emg_') and getattr(self,'emg_wizard',None) is not None:
            if key=='escape':self.quit=True
            elif key=='a':self.voice.say(emg_calibration.PROMPTS['manual'])
            elif key=='b':self.start_emg_calibration()
            if self.keyboard:self.keyboard.discard()
            return
        if self.state.startswith('wrist_') and getattr(self,'wrist_wizard',None) is not None:
            if key=='escape':self.quit=True
            elif key=='a':
                if self.state=='wrist_choose':self.set_wrist_method('occlusion')
                elif self.state=='wrist_manual':self.voice.say(wrist_calibration.PROMPTS['manual'])
                else:self.advance_wrist_calibration()
            elif key=='b':self.start_wrist_calibration()
            if self.keyboard:self.keyboard.discard()
            return
        if not self.quit and self.state=='confirm_stop' and key in ('a','b') and getattr(self,'worker',None):
            if key=='b':self.discard_and_retry()
            else:self.retry_requested=False;self.finish()
            if self.keyboard:self.keyboard.discard()
            return
        if self.state=='recording' and key=='a':
            result=self.stop_before_prompt()
            if self.keyboard:self.keyboard.discard()
            return result
        self.poll()
        if self.quit:return
        if key=='escape':
            self.quit=True
        elif key=='c' and self.state in ('device_confirm','idle','failed_wait','exhausted') and not getattr(self,'worker',None):
            if not self.selected_devices:self.selected_devices=tuple(self.scan_available)
            self.start_wrist_calibration()
        elif key=='d' and self.state in ('device_confirm','idle','failed_wait','exhausted') and not getattr(self,'worker',None):
            if not self.selected_devices:self.selected_devices=tuple(self.scan_available)
            self.start_emg_calibration()
        elif key=='enter' and self.state=='waiting_enter':
            self.state='idle';self.state_write()
            self.voice.say('已进入采集程序。点击准备本段查看任务参考图，或点击重复提示。')
        elif key=='b':
            if self.state=='device_confirm':
                self.scan_devices()
            elif self.state=='confirm_stop':
                self.discard_and_retry()
            elif self.state=='failed_wait':
                for message in self.last_help:self.voice.say(message)
            elif self.image and self.state in ('preparing','ready','idle'):
                self.announce()
        elif key=='a':
            if self.state=='device_confirm':
                if self.scan_ok:
                    self.selected_devices=tuple(self.scan_available)
                    if self.needs_wrist_calibration():
                        self.start_wrist_calibration()
                        if self.keyboard:self.keyboard.discard()
                        return
                    if self.needs_emg_calibration():
                        self.start_emg_calibration()
                        if self.keyboard:self.keyboard.discard()
                        return
                    self.state='idle';self.state_write()
                    self.voice.say('已确认本次采集设备。点击准备本段，查看任务参考图像并准备录制。')
                else:self.voice.say('未检测到任何采集设备。请检查连接后点击重新扫描。')
            elif self.state in ('idle','failed_wait'):
                self.prepare()
            elif self.state=='exhausted':
                try:self.reference_refresh(force=True)
                except (OSError,ValueError,RecursionError) as error:
                    self.reference_prepare_failed(error);return
                self.prepare()
            elif self.state in ('preparing','ready'):
                self.begin()
            elif self.state=='recording':
                self.stop_before_prompt()
            elif self.state=='confirm_stop':
                self.finish()
        if self.keyboard:
            self.keyboard.discard()

    def stop_worker(self):
        interrupt=getattr(self.voice,'interrupt',None)
        if callable(interrupt):interrupt()
        if self.quit:self.voice.silent=True
        if not self.worker:
            self.reference_release_pin()
            return
        if self.worker.poll() is None:
            try:
                self.command('STOP' if getattr(self,'keep_requested',False) else 'CANCEL')
                self.worker.wait(timeout=1800)
            except (self.OwnerExited,BrokenPipeError,subprocess.TimeoutExpired):
                if self.worker.poll() is None:
                    try:os.killpg(self.worker.pid,signal.SIGTERM)
                    except ProcessLookupError:pass
                    try:
                        self.worker.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        try:os.killpg(self.worker.pid,signal.SIGKILL)
                        except ProcessLookupError:pass
                        self.worker.wait()
        if self.state=='confirm_stop':
            self.retry_requested=True
            self.state='finalizing';self.state_write()
        self.poll()
        if self.worker:
            self.reap()

    def run(self):
        signal.signal(signal.SIGTERM,lambda *a:setattr(self,'quit',True))
        signal.signal(signal.SIGINT,lambda *a:setattr(self,'quit',True))
        try:
            self.state='starting'
            self.state_write()
            if not self.smoke:
                if self.console:self.keyboard=ConsoleKeyboard()
                else:self.keyboard=Keyboard()
            self.start_dashboard()
            self.state_write()
            self.scan_devices()
            if self.smoke:self.key('a')
            if self.smoke:
                self.key('a')
                deadline=time.monotonic()+25
                while not self.quit and self.state=='preparing' and time.monotonic()<deadline:
                    self.poll()
                    time.sleep(.1)
                if self.state!='ready':
                    raise RuntimeError('设备预检失败')
                self.key('b')
                self.key('a')
                if self.state!='recording':
                    raise RuntimeError('未进入采集状态')
                end=time.monotonic()+self.smoke
                while time.monotonic()<end and self.state=='recording':
                    self.poll()
                    time.sleep(.1)
                if self.state!='recording':
                    raise RuntimeError('测试采集中断')
                self.key('a')
                # Hardware smoke verifies a real pause at the operator confirmation.
                time.sleep(5)
                self.key('a')
                deadline=time.monotonic()+1800
                while self.worker and not self.quit and time.monotonic()<deadline:
                    self.poll()
                    time.sleep(.1)
                if self.success_count!=1:
                    raise RuntimeError('测试录制没有通过保存检查')
                return 0
            while not self.quit:
                self.poll_hotplug()
                self.poll()
                self.dashboard_tick()
                try:key=self.keyboard.read()
                except (OSError,RuntimeError):
                    if self.keyboard:self.keyboard.close()
                    self.keyboard=Keyboard();key=None
                if key:
                    try:self.key(key)
                    except self.OwnerExited as exc:self.owner_command_failed(exc)
            return self.exit_code
        except Exception as exc:
            traceback.print_exc()
            atomic_json(BASE/'runtime'/'last_error.json',dict(error=str(exc),time=time.time(),traceback=traceback.format_exc()))
            self.quit=True
            self.stop_worker()
            if isinstance(exc,CloudVoiceError):return 78
            self.voice.silent=False
            self.voice.cancelled=lambda:False
            try:
                self.voice.say('错误。'+str(exc)+'。请检查设备连接和日志。')
            except CloudVoiceError as audio_exc:
                print('AUDIO ERROR: '+str(audio_exc),flush=True)
                return 78
            except Exception as audio_exc:
                print('AUDIO ERROR: '+str(audio_exc),flush=True)
            return 1
        finally:
            self.quit=True  # Never reopen devices while unwinding an exception.
            self.stop_worker()
            self.close_dashboard()
            if self.keyboard:
                self.keyboard.close()
            self.state='stopped'
            self.state_write()
            self.voice.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--smoke',type=float,default=0,help='Real device test in diagnostics; does not consume a reference image')
    parser.add_argument('--console',action='store_true',help='Debug terminal: a/b + Enter, q/Ctrl+C to stop')
    parser.add_argument('--session-dir',help='Original framework shift root; default data/session-night')
    parser.add_argument('--image',help='Reference filename, stem, or path within configs/environments; start with this image, then continue in library order')
    parser.add_argument('--list-images',action='store_true')
    args=parser.parse_args()
    if args.list_images:
        try:
            for item in image_queue():print(item.relative_to(PROJECT))
        except BrokenPipeError:
            sys.stdout.close()
        sys.exit(0)
    (BASE/'runtime').mkdir(exist_ok=True)
    lock=(BASE/'runtime'/'launcher.lock').open('w')
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit('Another instance is already running')
    (BASE/'runtime'/'launcher.pid').write_text(str(os.getpid()))
    try:
        controller=Controller(args.smoke,args.image,args.session_dir,args.console)
    except CloudVoiceError as exc:
        print('CLOUD VOICE SAFETY STOP: '+str(exc),flush=True)
        sys.exit(78)
    except Exception as exc:
        traceback.print_exc()
        try:Speaker().say('启动失败。'+str(exc))
        except Exception:pass
        sys.exit(1)
    sys.exit(controller.run())
