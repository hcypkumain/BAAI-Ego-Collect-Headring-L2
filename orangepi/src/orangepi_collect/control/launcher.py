#!/usr/bin/env python3
"""A/B voice controller; no GUI and no dependency on the existing launcher."""
import os as _startup_os
if __name__=='__main__' and _startup_os.environ.pop('ORANGEPI_EARLY_WEB','')=='1':
    from service_entry import main as _startup_main
    _startup_main()
    raise SystemExit(0)

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
import threading
import uuid
import yaml
import ledger
import storage_paths

from project_paths import BASE, PROJECT, SRC, devices, CODE_BASE, PROFILE, COLLECTION_MODE, COLLECTION_CONTEXT
from capture_session_state import active_session
from device_discovery import fingerprint
from required_device_contract import build_contract, validate_scan
from scan_process import run_scan, ScanProcessError
from scan_registry import Registry, RegistryError, spawn_reserved
from scan_result import decode as decode_scan_result
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


def _full_capture_qualification_errors(path,summary):
    """Check frozen-device data and full-QC facts before task completion."""
    path=Path(path)
    try:
        selection=json.loads((path/'device_selection.json').read_text(encoding='utf-8'))
        if not isinstance(selection,dict):return ['冻结设备选择无效']
        from required_device_contract import resolve_selection,qualification_errors
        contract,errors,legacy=resolve_selection(selection.get('device_config'),selection)
        if errors or contract is None:return ['冻结必需设备合同无效：'+'；'.join(errors or ['缺少合同'])]
        report=json.loads((path/'qc_report.json').read_text(encoding='utf-8'))
        if not isinstance(report,dict):return ['完整质检报告无效']
        failures=list(qualification_errors(path,summary,contract,report,legacy=legacy) or [])
        from capture_qc_receipt import verify as verify_qc_receipt
        failures.extend(verify_qc_receipt(path) or [])
        return list(dict.fromkeys(failures))
    except (OSError,ValueError,TypeError,KeyError,RecursionError) as error:
        return ['本机完整质检凭据无法验证：'+str(error)]
    except ImportError as error:
        # Fail closed while a partial/old install lacks the shared wrapper.
        return ['本机完整质检判定不可用：'+str(error)]


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
        self.stop_pending=False
        self.stop_requested_path=None
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
        self.scan_lock=threading.Lock()
        self.camera_registry=Registry(CODE_BASE/'runtime')
        self.capture_owner_run=None
        self.capture_owner_released=False
        self.capture_owner_exit_record=None
        self.capture_registry_cleanup_pending=False
        self.preview_owner_run=None
        self.wrist_wizard=None
        self.emg_wizard=None

    def state_write(self):
        self.ui_revision=getattr(self,'ui_revision',0)+1
        self.voice.realtime=self.state in ('recording','countdown')
        self.voice.silent=self.state=='recording' or self.quit
        write_error=None;publish_error=None
        try:
            atomic_json(BASE/'runtime'/'controller.json',dict(pid=os.getpid(),state=self.state,
                session=active_session(self),image=str(self.image) if self.image else None,
                review_session=str(getattr(self,'last_review_path',None)) if getattr(self,'last_review_path',None) else None,
                collection_mode=COLLECTION_MODE,collection_context=COLLECTION_CONTEXT,
                success_count=self.success_count,selected_image=self.selected_image,selected_devices=getattr(self,'selected_devices',None),
                wrist_calibration_phase=getattr(getattr(self,'wrist_wizard',None),'phase',None),
                emg_calibration_phase=getattr(getattr(self,'emg_wizard',None),'phase',None),time=time.time()))
            if BASE!=CODE_BASE:
                atomic_json(CODE_BASE/'runtime/controller.json',json.loads((BASE/'runtime/controller.json').read_text()))
        except Exception as error:write_error=error
        finally:
            try:self.ui_publish(force=bool(getattr(self,'owner_exit_recovered',False)))
            except Exception as error:publish_error=error
            if publish_error is not None and getattr(self,'owner_exit_recovered',False):
                try:
                    dashboard=getattr(self,'dashboard',None)
                    if dashboard is not None:
                        fallback=dict(getattr(dashboard,'snapshot',{}) or {})
                        fallback.update(state='error',revision=getattr(self,'ui_revision',0),
                            session=None,worker=dict(state='error',recording_active=False,
                            errors=list(getattr(self,'last_help',[]))),busy=False,
                            collection_mode=COLLECTION_MODE,collection_context=COLLECTION_CONTEXT)
                        dashboard.publish(fallback);publish_error=None
                except Exception as error:publish_error=error
        if write_error is not None:
            if getattr(self,'owner_exit_recovered',False):
                self.owner_exit_persist_error=str(write_error)[:200]
            else:raise write_error
        if publish_error is not None:
            if getattr(self,'owner_exit_recovered',False):
                self.owner_exit_persist_error=str(publish_error)[:200]
            else:raise publish_error

    def switch_collection_mode(self,mode):
        from collection_profiles import provision
        if mode not in ('emg','eeg'):raise ValueError('请选择脑电或肌电')
        if mode==COLLECTION_MODE:return
        if self.state not in ('device_confirm','idle','failed_wait','exhausted') or self.worker:
            raise ValueError('请先结束或放弃当前段，再切换采集模式')
        privacy=self.dashboard.privacy.snapshot()
        if privacy.get('required') or privacy.get('stop_confirmed') is not True:
            raise ValueError('请等待设备确认停止后切换')
        uploads=getattr(self.dashboard,'cloud_uploads',None)
        previous=self.state
        self.mode_switch_target=mode
        if uploads is not None:
            with uploads.lock:
                if uploads.active:raise ValueError('请等待当前上传结束或先暂停上传，再切换模式')
                self.state='mode_switching';self.state_write()
        else:self.state='mode_switching';self.state_write()
        try:provision(CODE_BASE,PROFILE,mode)
        except Exception:
            self.state=previous;self.state_write();raise
        if self.stop_preview() is False:raise ValueError('预览设备尚未全部退出')
        self.switch_requested=mode
        self.quit=True

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
        if '即时质检' in text:
            # This is a saved-data verdict, not an unidentified device crash.
            self.last_help=list(dict.fromkeys(getattr(self,'last_help',[])+list(errors)))
            reasons=[str(item).split('处理建议：',1)[0] for item in errors[:2]]
            self.voice.say('。'.join(reasons)+'。具体原因和处理建议已在录制结果中按左右侧显示。')
            return
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
        from speech_prompts import task_announcement
        prompt=task_announcement(self.reference_image_title())
        self.voice.say(prompt)

    def prefetch_reference_announcement(self):
        from speech_prompts import task_announcement
        prefetch=getattr(self.voice,'prefetch',None)
        if callable(prefetch):prefetch({'task_announcement':task_announcement(self.reference_image_title())},priority='urgent')

    def required_capture_error(self,status=None):
        contract=getattr(self,'required_device_contract',None) or {}
        required=set(contract.get('required_devices',()))
        if contract.get('version')!='required-devices-v1' or not required:return '本段必需设备合同无效，请重新扫描并准备'
        selected=set(getattr(self,'selected_devices',None) or ())
        missing=sorted(required-selected)
        if missing:return '本段未选中全部必需设备：'+','.join(missing)
        status=status if isinstance(status,dict) else self.worker_status()
        if status.get('required_device_contract_version')!='required-devices-v1':
            return '采集进程未提供必需设备实时就绪证明，请重新准备本段'
        if set(status.get('required_devices') or ())!=required:
            return '采集进程冻结的必需设备清单与本段配置不一致，请重新准备'
        if status.get('capture_session')!=getattr(getattr(self,'path',None),'name',None):
            return '采集进程状态与当前会话不匹配，请重新准备本段'
        stamp=status.get('time')
        if type(stamp) not in (int,float) or time.time()-stamp<0 or time.time()-stamp>2.5:
            return '必需设备实时状态已过期，请等待重新检查后再开始'
        pid=status.get('pid')
        try:
            process=(Path('/proc')/str(pid)/'cmdline').read_bytes()
            if type(pid) is not int or b'acquisition/capture.py' not in process:return '采集进程在线证明无效，请重新准备本段'
        except (OSError,TypeError):return '采集进程在线证明无效，请重新准备本段'
        if status.get('required_devices_ready') is not True:
            missing=status.get('required_devices_missing') or sorted(required)
            return '必需设备未保持实时就绪：'+','.join(missing)
        readiness=status.get('required_device_readiness')
        if not isinstance(readiness,dict) or set(readiness)!=required or any(
                not isinstance(readiness.get(name),dict) or readiness[name].get('ready') is not True
                or readiness[name].get('fresh') is not True for name in required):
            return '采集进程缺少逐设备实时就绪证明，请重新准备本段'
        return None

    def required_scan_error(self,config=None):
        config=config or getattr(self,'web_config',None) or devices()
        try:contract=build_contract(config,mode=config.get('collection_mode'))
        except (ValueError,TypeError) as error:return str(error)
        missing=validate_scan(contract,getattr(self,'last_scan',[]))
        missing=sorted(set(missing)|(set(contract['required_devices'])-set(getattr(self,'selected_devices',None) or ())))
        self.required_device_contract=contract;self.required_devices_missing=missing
        return ('必需设备未全部在线或没有有效数据：'+','.join(missing)) if missing else None

    def prepare(self):
        if getattr(self,'capture_registry_cleanup_pending',False):
            self.state='failed_wait';self.last_help=['设备退出已确认，但所有权记录清理未确认；为避免重复占用硬件，暂不能开始下一段，请联系维护人员。']
            try:self.state_write()
            except OSError:pass
            return False
        if not self.retry_pending_owner(force=True):
            self.last_help=['上次设备清理尚未确认'];self.state_write();return False
        if (BASE/'runtime/recording_reset_in_progress.json').exists():
            self.last_help=['旧采集数据正在归档，请等待维护完成后再准备。'];self.state_write();return False
        if self.worker is not None:
            self.last_help=['当前本段仍由采集进程持有，请结束或取消准备后再重试。']
            return False
        if self.stop_preview() is False:
            self.last_help=['预览设备尚未全部退出'];self.state_write();return False
        if time.monotonic()-getattr(self,'last_device_scan',0)>20 or not getattr(self,'scan_ok',False):
            if not self.scan_devices(announce=False,quiet=True):
                self.state='device_confirm';self.state_write();return False
        required_error=self.required_scan_error()
        if required_error:
            self.state='device_confirm';self.last_help=[required_error+'。请检查后重新扫描。'];self.state_write()
            self.voice.say(self.last_help[0]);return False
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
        try:
            self.required_device_contract=build_contract(resolved,self.selected_devices,resolved.get('collection_mode'))
        except (ValueError,TypeError) as error:
            self.state='device_confirm';self.last_help=[str(error)+'。请重新扫描并确认设备。'];self.state_write()
            self.voice.say(self.last_help[0]);return False
        if (set(self.selected_devices or ()).intersection(emg_calibration.SLOTS)
                and resolved.get('emg_calibration',{}).get('enabled',False)
                and not resolved.get('emg_calibration_status',{}).get('valid',False)):
            self.start_emg_calibration();return
        if (set(self.selected_devices or ()).intersection(wrist_calibration.SLOTS)
                and resolved.get('wrist_calibration',{}).get('enabled',False)
                and not resolved.get('wrist_calibration_status',{}).get('valid',False)):
            self.start_wrist_calibration();return
        self.capture_owner_released=False
        try:self.capture_owner_run=self.camera_registry.reserve('capture')
        except (RegistryError,OSError) as exc:
            self.last_help=[str(exc)];self.state_write();return False
        spawn_attempted=False
        self.keep_requested=False;self.owner_command_error=None;self.discard_requested=False
        new_path=None
        try:
            self.path=None
            selection=self.reference_begin_preparation()
            self.prefetch_reference_announcement()
            self.attempted.add(str(self.image))
            self.path,identity=self.reference_reserve_directory(root);new_path=self.path
            # Freeze resolved devices and the exact task/queue identity for this owner.
            self.session_devices=resolved
            device_selection=dict(selected=list(self.selected_devices),device_config=resolved,collection_mode=COLLECTION_MODE,collection_context=COLLECTION_CONTEXT,
                required_device_contract=self.required_device_contract)
            atomic_json(self.path/'device_selection.json',device_selection)
            metadata=dict(identity,collection_mode=COLLECTION_MODE,collection_context=COLLECTION_CONTEXT,reference_image=selection['asset']['relative_path'],
                reference_sha256=selection['asset']['sha256'],reference_selection=selection,
                required_device_contract=self.required_device_contract,
                test=bool(self.smoke),stim=False,pygame=False,
                device_config=resolved,selected_devices=list(self.selected_devices),storage_config=storage_paths.config(),
                keyboard_flow='Touch workflow: prepare and view reference without time limit, impedance, manual camera check, return to preparation, separate formal start, 5-to-1 countdown, hold 3s to stop, save or retry same task',
                timestamp_note='Shared host start gate; camera ROS source stamps and EMG host arrival stamps retained. No hardware synchronization is claimed.')
            from qc_deferred import load as qc_execution
            metadata['qc_mode']=qc_execution(BASE)['mode']
            self.reference_set_raw_metadata(metadata)
            atomic_json(self.path/'meta.json',metadata)
            atomic_json(self.path/'reference_queue.json',self._reference_active['entry']['snapshot'])
            self.log=(self.path/'worker.log').open('w')
            spawn_attempted=True
            self.worker=spawn_reserved(self.camera_registry,self.capture_owner_run,'capture',[str(BASE/'.venvs/framework/bin/python'),'-u',str(SRC/'framework/framework_recorder.py'),'--output',str(self.path),'--wait-for-save'],
                stdin=subprocess.PIPE,stdout=self.log,stderr=subprocess.STDOUT,text=True)
        except Exception as error:
            if not spawn_attempted and self.capture_owner_run:
                try:self.camera_registry.cancel_unspawned(self.capture_owner_run);self.capture_owner_run=None
                except (RegistryError,OSError):pass
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
        self.stop_pending=False
        self.stop_requested_path=None
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
        contract_error=self.required_capture_error(self.worker_status())
        if contract_error:
            self.voice.say(contract_error+'。请重新准备设备后再开始。')
            return False
        if 'eeg' in (self.selected_devices or ()) and not impedance_allows_recording(self.worker_status().get('eeg_impedance')):
            self.voice.say('请先结束阻抗检查、确认结果，并等待普通脑电采样恢复。')
            return False
        checks=getattr(dashboard,'camera_checks',None)
        if checks is not None and not checks.approved():
            self.voice.say('请先完成全部相机同屏检查，并确认头环和腕部相机方向。')
            return False
        self.owner_exit_recovered=False;self.owner_exit_proof=None;self.owner_exit_persist_error=None
        self.stop_pending=False;self.stop_retry_authorized=False;self.stop_escalation_phase=None
        self.stop_force_deadline=None
        privacy.arm(str(self.path.resolve()),worker_pid=self.worker_status().get('pid'),
                    adapter_pid=self.worker.pid,owner_run=self.capture_owner_run)
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
        try:self.camera_registry.verify_owner(self.capture_owner_run,'capture')
        except RegistryError:
            self.last_help=['设备所有权未确认，未开始采集'];self.state_write();return False
        if 'eeg' in self.selected_devices and not impedance_allows_recording(self.worker_status().get('eeg_impedance')):
            self.voice.say('请先完成脑电阻抗检查，并确认检测已结束、设备配置已恢复。')
            return
        if self.worker_status().get('state')!='ready':
            self.voice.say('设备尚未就绪，请等待准备完成后再点击开始采集。')
            return
        contract_error=self.required_capture_error(self.worker_status())
        if contract_error:
            self.voice.say(contract_error+'。本次没有开始采集，请重新准备。')
            return False
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
        try:self.camera_registry.verify_owner(self.capture_owner_run,'capture')
        except RegistryError:
            self.last_help=['设备所有权未确认，未开始采集'];self.state_write();return False
        contract_error=self.required_capture_error(self.worker_status())
        if contract_error:
            self.last_help=[contract_error+'。本次没有开始采集，请重新准备。'];self.state='ready';self.state_write();return False
        try:atomic_json(self.path/'screen_privacy_gate.json',privacy.commit())
        except Exception as error:
            if not privacy.committed:raise
            return self._stop_after_start_failure(error)
        if privacy.phase!='arming':
            self.stop_before_prompt();return False
        try:self.command('START')
        except (self.OwnerExited,OSError) as error:return self._stop_after_start_failure(error)
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            status=self.worker_status()
            if status.get('state')=='recording':
                stop_queued=privacy.phase=='stopping'
                if not stop_queued:privacy.recording()
                self.recording_since=time.time()
                self.state='recording'
                self.state_write()
                print('RECORDING '+str(self.path),flush=True)
                if privacy.cancelled or stop_queued or privacy.phase=='stopping':return self.stop_before_prompt()
                return
            if status.get('state') in ('error','success') or self.worker.poll() is not None:
                if status.get('state')=='error' and privacy.committed:
                    return self._stop_after_start_failure(RuntimeError('采集进程在START确认前报告错误'))
                self.poll()
                return
            time.sleep(.05)
        return self._stop_after_start_failure(RuntimeError('开始采集确认超时'))

    def _stop_after_start_failure(self,error):
        message='开始采集未获有效确认：'+str(error)+'。已请求本段停止并保留数据，不计为成功。'
        self.owner_command_error=message;self.keep_requested=False;self.state='error';self.last_help=[message]
        try:
            self.stop_before_prompt()
        except self.OwnerExited as owner_error:
            self.owner_command_failed(owner_error)
            return False
        try:self.state_write()
        except OSError:pass
        self.voice.say(message)
        return False

    def finish(self):
        self.keep_requested=True
        if getattr(self,'worker',None) is not None and self.worker.poll() is None:
            self.command('KEEP')
        self.state='finalizing'
        self.finalize_deadline=time.monotonic()+1800
        self.state_write()
        from qc_deferred import load as qc_execution
        mode=qc_execution(BASE)['mode']
        self.voice.say('采集已停止，正在保存并'+('完成肌电/脑电检查，其余待主机质检。' if mode=='emg_only' else '标记待主机质检。' if mode=='deferred' else '检验，请等待。'))

    def discard_recording(self):
        if self.state == 'recording':
            if not self.stop_before_prompt():return False
        if self.state != 'confirm_stop':raise ValueError('当前没有可放弃的录制')
        self.discard_return_overview=True
        return self.discard_and_retry()

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
        self.voice.say('正在停止设备并清理本段文件，请等待。清理后由您手动准备下一段。')
        self.poll()

    def poll_discard(self):
        if self.worker.poll() is None:
            if time.monotonic()<=self.discard_deadline:return
            # A wedged owner must release every device before any file removal.
            os.killpg(self.worker.pid,signal.SIGKILL);self.worker.wait(timeout=5)
        path=self.path
        if not self.owner_finished():return
        self._reference_reap_outcome='discarded'
        if not self.reap():return
        try:
            self.recording_store.discard_current(path)
        except (OSError,ValueError) as error:
            self.discard_requested=False;self.retry_requested=False
            self.state='failed_wait';self.last_help=['本段文件未能清理：'+str(error)+'。请处理存储问题后重试。']
            self.state_write();self.voice.say(self.last_help[0]);return
        self.discard_requested=False;self.retry_requested=False;self.path=None
        self.last_completed=None
        if getattr(self,'discard_return_overview',False):
            self.reference_release_pin();self._reference_active=None;self._reference_retry=None
            self._reference_manual_id=None;self.image=None;self.selected_image=None
        self.discard_return_overview=False
        self.preparation_cancelled_at=time.time()
        if getattr(self,'last_review_path',None)==path:
            import recording_review
            self.last_review_path=recording_review.latest_recording(self.recording_store.roots()[0])
        self.reference_refresh(force=True)
        self.state='idle';self.exit_code=0;self.state_write()
        self.voice.say('本段已放弃，文件已清理。请手动点击准备本段。')

    def terminate_capture_owner(self,sig):
        """Signal only the currently registered capture process group."""
        worker=getattr(self,'worker',None);run=getattr(self,'capture_owner_run',None)
        privacy=getattr(getattr(self,'dashboard',None),'privacy',None)
        if worker is None or not run or privacy is None or not privacy.committed:
            return False
        try:
            owner=self.camera_registry.verify_signalable_group(
                run,'capture',allow_exited_leader=(sig==signal.SIGKILL))
            proof=privacy.start_proof or {}
            if (owner.get('run')!=proof.get('owner_run') or owner.get('owner_pid')!=proof.get('controller_pid')
                    or owner.get('owner_start')!=proof.get('controller_start_ticks')
                    or owner.get('wrapper_pid')!=worker.pid or owner.get('wrapper_pid')!=proof.get('adapter_pid')
                    or owner.get('wrapper_start')!=proof.get('adapter_start_ticks')
                    or owner.get('pgid')!=worker.pid or owner.get('sid')!=worker.pid):
                raise RegistryError('当前设备进程身份与隐私会话不匹配')
            os.killpg(owner['pgid'],sig)
            return True
        except (RegistryError,OSError,ProcessLookupError,ValueError,TypeError) as error:
            self.last_help=['未能验证当前设备进程组，未发送结束信号：'+str(error)]
            return False

    def stop_before_prompt(self,target=None):
        privacy=getattr(getattr(self,'dashboard',None),'privacy',None)
        if privacy is not None:
            privacy.stopping(target,allow_same_session=True)
        interrupt=getattr(self.voice,'interrupt',None)
        if callable(interrupt):interrupt()
        if getattr(self,'stop_pending',False):
            return True
        if getattr(self,'stop_force_deadline',None) is None:
            self.stop_force_deadline=time.monotonic()+1800
        retry_authorized=getattr(self,'stop_retry_authorized',False)
        if getattr(self,'stop_requested_path',None)!=self.path or retry_authorized:
            try:self.command('STOP')
            except self.OwnerExited as exc:
                self.owner_command_failed(exc)
                return False
            self.stop_requested_path=self.path
            self.stop_retry_authorized=False
            self.stop_escalation_phase=None
        self.stop_pending=True
        self.stop_deadline=time.monotonic()+45
        self.state_write()
        return True

    def poll_stop(self):
        privacy=getattr(getattr(self,'dashboard',None),'privacy',None)
        stop_pending=getattr(self,'stop_pending',False)
        if not stop_pending and (privacy is None or not privacy.committed):return
        if (getattr(self,'worker',None) is not None and self.worker.poll() is not None
                and not getattr(self,'capture_owner_released',False)):
            self.owner_finished()
        try:proof=json.loads((self.path/'capture_complete.json').read_text())
        except (OSError,ValueError,TypeError):proof=None
        if not isinstance(proof,dict):proof=None
        # ui_publish may already have validated this same receipt.
        confirmed=privacy is not None and not privacy.committed
        if proof and privacy is not None and privacy.committed:
            confirmed=privacy.confirm_stopped(proof)
        if not confirmed and privacy is not None and privacy.committed:
            try:owner_proof=json.loads((self.path/'owner_group_exit.json').read_text())
            except (OSError,ValueError,TypeError):owner_proof=getattr(self,'owner_exit_proof',None)
            if isinstance(owner_proof,dict) and privacy.confirm_owner_exited(owner_proof):
                confirmed=True;self.owner_exit_recovered=True
        if confirmed:
            self.stop_pending=False
            self.stop_force_deadline=None
            if getattr(self,'owner_exit_recovered',False):
                self.state='error';self.last_help=['采集设备进程组已确认退出，但本段缺少有效的原生收尾证明；数据保留并标记失败，请检查日志后重新准备。']
                if getattr(self,'owner_exit_persist_error',None):
                    self.last_help.append('停止恢复诊断未写入磁盘：'+self.owner_exit_persist_error)
                try:self.state_write()
                except OSError:self.last_help.append('错误页面状态暂未写入磁盘。')
                self.voice.say(self.last_help[0])
            elif proof and proof.get('summary_status')=='error':
                self.state='error';self.last_help=['采集已停止，但设备或文件收尾发生错误；数据未标记为成功，请查看日志。']
                self.state_write();self.voice.say(self.last_help[0])
            else:
                self.state='confirm_stop';self.state_write()
                self.voice.say('采集已停止。请点击确认保存，或选择重采本段。')
        elif stop_pending and time.monotonic()>self.stop_deadline:
            if privacy is not None:privacy.fault('停止收尾尚未确认，仍在等待本段原生设备结束')
            # Forty-five seconds is a warning boundary only. Camera EOS and
            # ffprobe may legitimately take several minutes; never cut them
            # off automatically at this point. A deliberate STOP can retry.
            self.stop_pending=False;self.stop_retry_authorized=True;self.stop_deadline=float('inf')
            self.stop_escalation_phase='waiting_user_retry'
            self.last_help=['停止状态尚未确认；设备可能仍在完成文件收尾，数据继续保留。请检查进度后长按 STOP 重试。']
            self.state_write()
        hard_deadline=getattr(self,'stop_force_deadline',None)
        if (privacy is not None and privacy.committed and not getattr(self,'owner_exit_recovered',False)
                and isinstance(hard_deadline,(int,float)) and time.monotonic()>=hard_deadline):
            phase=getattr(self,'stop_escalation_phase',None)
            if phase not in ('hard_term','hard_kill','hard_exhausted'):
                self.stop_escalation_phase='hard_term'
                if self.terminate_capture_owner(signal.SIGTERM):self.stop_force_deadline=time.monotonic()+2
                else:
                    self.stop_escalation_phase='hard_exhausted';self.stop_force_deadline=None
                    self.last_help=['长时间停止超时；没有向无法验证的设备进程组发送信号。隐私遮挡保持，请联系维护人员。']
                    self.state_write()
            elif phase=='hard_term':
                self.stop_escalation_phase='hard_kill'
                if self.terminate_capture_owner(signal.SIGKILL):self.stop_force_deadline=time.monotonic()+2
                else:
                    self.stop_escalation_phase='hard_exhausted';self.stop_force_deadline=None
                    self.last_help=['长时间停止超时；设备进程身份已变化，未发送强制结束信号。隐私遮挡保持，请联系维护人员。']
                    self.state_write()
            else:
                self.stop_escalation_phase='hard_exhausted';self.stop_force_deadline=None
                self.last_help=['已向经过核验的设备进程组发送有界结束信号，但进程组仍未退出。隐私遮挡保持，请联系维护人员。']
                self.state_write()

    def scan_devices(self,announce=True,quiet=False):
        if not self.scan_lock.acquire(False):return False
        try:
            if not self.retry_pending_owner(force=True):return False
            if self.worker or self.state in ('preparing','recording','countdown','finalizing','confirm_stop'):
                self.last_help=['设备正在准备、采集或收尾，不能扫描'];return False
            return self._scan_devices(announce,quiet)
        finally:self.scan_lock.release()

    def _scan_devices(self,announce=True,quiet=False):
        from required_device_contract import build_contract,validate_scan
        if self.stop_preview() is False:
            self.last_help=['预览设备尚未全部退出'];self.scan_retry_pending=True;self.state_write();return False
        previous_state=self.state
        scanned_fingerprint=fingerprint(devices().get('auto_discovery',{}))
        fingerprint_wire=json.loads(json.dumps(scanned_fingerprint));request_id=uuid.uuid4().hex
        scan_env=dict(os.environ,ORANGEPI_SCAN_REQUEST=json.dumps(dict(id=request_id,fingerprint=fingerprint_wire)))
        scan_run=None;scan_nonce=None
        self.state='scanning';self.state_write()
        try:
            output,scan_run,scan_nonce=run_scan([str(BASE/'.venvs/hardware/bin/python'),str(SRC/'acquisition/scan_devices.py')],self.camera_registry,timeout=15,allow_partial=True,env=scan_env,return_identity=True)
            if getattr(self,'quit',False) or self.state!='scanning' or getattr(self,'switch_requested',None):return False
            scan=decode_scan_result(output,scan_run,scan_nonce,request_id,fingerprint_wire)
            if fingerprint(devices().get('auto_discovery',{}))!=scanned_fingerprint:
                raise ValueError('扫描期间设备发生变化，旧结果已丢弃')
            if not isinstance(scan,list) or any(not isinstance(item,dict)
                    or not isinstance(item.get('name'),str) or not item['name']
                    or not isinstance(item.get('label'),str)
                    or type(item.get('connected')) is not bool for item in scan):
                raise ValueError('设备扫描结果格式无效')
            self.web_config=devices()
            if fingerprint(self.web_config.get('auto_discovery',{}))!=scanned_fingerprint:
                raise ValueError('扫描结果发布前设备发生变化')
            self.required_device_contract=build_contract(self.web_config,mode=self.web_config.get('collection_mode'))
        except (OSError,ValueError,subprocess.TimeoutExpired,ScanProcessError) as exc:
            if isinstance(exc,ScanProcessError) and exc.cleanup_pending:
                self.remember_pending_owner(exc)
            elif isinstance(exc,ScanProcessError) and getattr(exc,'ownership_blocked',False):
                self.remember_pending_owner(exc)
            reason=('设备扫描超时' if isinstance(exc,subprocess.TimeoutExpired)
                else '设备扫描结果不是有效 JSON' if isinstance(exc,json.JSONDecodeError) else str(exc))
            print('DEVICE SCAN failed '+repr(exc),flush=True)
            if isinstance(exc,ScanProcessError) and getattr(exc,'ownership_blocked',False):
                message='设备所有权恢复未确认，设备状态未知，已保持采集锁定：'+reason+'。请重试恢复或联系维护人员。'
            else:message='设备扫描未完成：'+reason+'。请检查设备后重新扫描。'
            self.last_help=[message]
            self.scan_ok=False;self.scan_available=[];self.selected_devices=None
            self.required_devices_missing=list(getattr(self,'required_device_contract',{}).get('required_devices',[]))
            self.scan_retry_pending=True
            # Retain identities for diagnosis, without presenting old readiness
            # as current evidence or permitting a stale device confirmation.
            partial=None
            if isinstance(exc,ScanProcessError) and exc.partial_output:
                try:
                    partial=decode_scan_result(exc.partial_output,exc.run,getattr(exc,'nonce',None),request_id,fingerprint_wire)
                except (ValueError,TypeError):pass
            if partial is not None:
                self.last_scan=[dict(item,last_probe_connected=item['connected'],connected=False,status='cleanup_pending',
                    error='本轮探测'+('已取到数据' if item['connected'] else '未确认')+'；设备仍在清理，暂不可使用') for item in partial]
            else:
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
        self.scan_retry_pending=False
        self.last_help=[]
        self.last_device_scan=time.monotonic()
        # A plug/unplug during the scan must trigger another scan, not be lost.
        self.usb_fingerprint=scanned_fingerprint
        atomic_json(BASE/'runtime/devices.json',scan)
        self.scan_available=[item['name'] for item in scan if item['connected']]
        missing_required=validate_scan(self.required_device_contract,scan)
        self.required_devices_missing=missing_required
        self.scan_ok=not missing_required
        connected=[item['label'] for item in scan if item['connected']]
        missing=[item['label'] for item in scan if not item['connected']]
        self.state=previous_state if not announce and not changed and self.scan_ok else 'device_confirm'
        if changed:self.selected_devices=None
        if missing_required:
            labels={row['name']:row.get('label',row['name']) for row in scan}
            self.last_help=['必需设备未全部在线或没有有效数据：'+'、'.join(labels.get(name,name) for name in missing_required)+'。请检查后重新扫描。']
        else:self.last_help=[]
        self.state_write()
        if quiet:return True
        if not announce and not changed:return True
        self.voice.say('Orange Pi 采集程序。当前接入的数据采集设备：'+('，'.join(connected) or '尚未发现采集设备')+'。')
        if missing:self.voice.say('未收到有效数据：'+'，'.join(missing)+'。请检查连接。')
        if any(x.get('status')=='no_data' and x['name'].startswith('emg') for x in scan):
            self.voice.say('肌电接收器已识别，但未收到有效肌电。请检查臂环电源和连接，程序会自动重试。')
        if missing_required:
            labels={row['name']:row.get('label',row['name']) for row in scan}
            self.voice.say('必需采集设备未全部在线。当前不能确认设备，也不能开始采集。请检查连接后重新扫描。')
            self.voice.say('暂缺设备：'+'、'.join(labels.get(name,name) for name in missing_required)+'。')
        else:
            self.voice.say('必需采集设备均已在线。请点击确认设备，或选择重新扫描。')
        return True

    def needs_wrist_calibration(self):
        if not set(getattr(self,'selected_devices',None) or ()).intersection(wrist_calibration.SLOTS):
            return False
        config=devices()
        return config.get('wrist_calibration',{}).get('enabled',False) and not config.get('wrist_calibration_status',{}).get('valid',False)

    def start_wrist_calibration(self):
        if getattr(self,'worker',None):
            return  # The capture worker has exclusive ownership until released.
        if self.stop_preview() is False:raise ValueError('预览设备尚未全部退出')
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
        if self.stop_preview() is False:raise ValueError('预览设备尚未全部退出')
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
        if self.stop_preview(clear=False) is False:
            self.last_help=['预览设备尚未全部退出'];self.state_write();return
        wizard=getattr(self,'wrist_wizard',None)
        if wizard is None or getattr(self,'worker',None):return
        if not wizard.unchanged(wrist_calibration.boot_id(),wrist_calibration.current_stamp(wizard.config)):
            self.calibration_connection_changed();return
        try:
            output,exit_code=run_scan([str(BASE/'.venvs/hardware/bin/python'),str(SRC/'acquisition/calibration_probe.py')],
                self.camera_registry,kind='calibration',input_text=json.dumps(wizard.config),timeout=12,allow_nonzero=True,return_code=True)
            data=json.loads(output)
            if data.get('error')=='devices_changed':
                self.calibration_connection_changed();return
            if exit_code!=0 or not data.get('ok'):
                raise wrist_calibration.CalibrationError(data.get('error','probe_failed'))
            if not wizard.unchanged(data['boot_id'],data['stamp']) or not wizard.unchanged(wrist_calibration.boot_id(),wrist_calibration.current_stamp(wizard.config)):
                self.calibration_connection_changed();return
            wizard.accept(data['statistics'])
        except (wrist_calibration.CalibrationError,ValueError,KeyError,subprocess.TimeoutExpired,ScanProcessError) as exc:
            if isinstance(exc,ScanProcessError) and exc.cleanup_pending:
                self.remember_pending_owner(exc);self.state_write()
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
        if self.stop_preview() is False:raise ValueError('预览设备尚未全部退出')
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
        if self.stop_preview() is False:raise ValueError('预览设备尚未全部退出')
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
        retry=(pending or getattr(self,'scan_retry_pending',False)) and now-getattr(self,'last_device_scan',0)>=settings.get('retry_no_data_seconds',15)
        if changed or retry:
            self.scan_devices(announce=False)
            if self.keyboard:self.keyboard.discard()

    def _persist_owner_exit_proof(self):
        record=getattr(self,'capture_owner_exit_record',None)
        liveness=getattr(self,'capture_owner_exit_liveness',None) or {}
        privacy=getattr(getattr(self,'dashboard',None),'privacy',None)
        if not isinstance(record,dict) or privacy is None or not privacy.committed:return True
        start=privacy.start_proof
        if not isinstance(start,dict) or not self.path:return False
        try:normal=json.loads((self.path/'capture_complete.json').read_text())
        except (OSError,ValueError,TypeError):normal=None
        if isinstance(normal,dict) and privacy.confirm_stopped(normal):return True
        path=self.path/'owner_group_exit.json'
        try:
            if self.worker is not None and self.worker.poll() is None:return False
            from scan_registry import group_exists
            pgid=record.get('pgid')
            if type(pgid) is not int or group_exists(pgid):return False
            if (record.get('phase') not in ('registered','terminal') or record.get('kind')!='capture'
                    or record.get('run')!=getattr(self,'capture_owner_exit_run',None)
                    or record.get('boot')!=start.get('boot_id')
                    or record.get('owner_pid')!=start.get('controller_pid')
                    or record.get('owner_start')!=start.get('controller_start_ticks')
                    or record.get('wrapper_pid')!=start.get('adapter_pid')
                    or record.get('wrapper_start')!=start.get('adapter_start_ticks')
                    or record.get('pgid')!=record.get('wrapper_pid') or record.get('sid')!=record.get('wrapper_pid')
                    or record.get('run')!=start.get('owner_run')):
                return False
            observed=time.monotonic_ns()
            proof=dict(schema_version=1,policy='capture-owner-group-exit-v1',
                session_id=str(self.path.resolve()),nonce=start.get('nonce'),
                privacy_instance=start.get('privacy_instance'),privacy_generation=start.get('privacy_generation'),
                owner_run=start.get('owner_run'),screen_privacy=start,
                registry_run=record.get('run'),registry_nonce=record.get('nonce'),registry_kind=record.get('kind'),
                registry_finish_verified=liveness.get('registry_cleanup_confirmed') is True,
                registry_liveness_verified=liveness.get('liveness_verified') is True,
                registry_cleanup_confirmed=liveness.get('registry_cleanup_confirmed') is True,
                group_absent=True,adapter_reaped=True,
                boot_id=record.get('boot'),controller_pid=record.get('owner_pid'),
                controller_start_ticks=record.get('owner_start'),adapter_pid=record.get('wrapper_pid'),
                adapter_start_ticks=record.get('wrapper_start'),worker_pid=start.get('worker_pid'),
                worker_start_ticks=start.get('worker_start_ticks'),pgid=record.get('pgid'),sid=record.get('sid'),
                native_shutdown_confirmed=False,native_qc_success=False,
                worker_exit_code=self.worker.returncode if self.worker is not None else None,
                observed_monotonic_ns=observed,recorded_at_unix=time.time())
            if not privacy.confirm_owner_exited(proof):return False
            self.owner_exit_recovered=True;self.owner_exit_proof=proof
            try:atomic_json(path,proof)
            except (OSError,ValueError,TypeError) as error:
                # The verified in-memory process-group fact is sufficient to
                # leave the permanent privacy cover; this diagnostic write is
                # advisory and never changes the unsuccessful task verdict.
                self.owner_exit_persist_error=str(error)[:200]
            return True
        except (OSError,ValueError,TypeError,RegistryError):return False

    def owner_finished(self):
        if getattr(self,'capture_owner_released',False):
            return self._persist_owner_exit_proof()
        run=getattr(self,'capture_owner_run',None)
        record=None;witness=None
        try:
            if not run:raise RegistryError('缺少本段设备所有权记录')
            record=self.camera_registry.inspect()
            if (not isinstance(record,dict) or record.get('run')!=run or record.get('kind')!='capture'
                    or (record.get('phase')!='registered'
                        and not (record.get('phase')=='terminal' and record.get('terminal_reason')=='normal_confirmed'))):
                raise RegistryError('本段设备所有权记录已变化')
            evidence=self.camera_registry.inspect_evidence()
            witness=evidence.get('witness') or record.get('terminal_witness')
            if evidence.get('errors') or not isinstance(witness,dict):
                raise RegistryError('本段设备独立见证无法读取')
            liveness=self.camera_registry.verify_exited(run,'capture')
            self.camera_registry.finish(run)
            if self.camera_registry.inspect() is not None:
                raise RegistryError('设备所有权终结状态未清除')
        except (RegistryError,OSError) as exc:
            try:liveness=self.camera_registry.verify_exited(
                run,'capture',record if isinstance(record,dict) else None,
                witness if isinstance(witness,dict) else None)
            except (RegistryError,OSError) as verify_error:
                message='设备进程组尚未确认退出，隐私遮挡保持；停止状态检查失败：'+str(verify_error)
                if getattr(self,'last_help',None)!=[message]:
                    self.last_help=[message]
                    try:self.state_write()
                    except OSError:pass
                return False
            self.capture_owner_exit_run=run;self.capture_owner_exit_record=liveness
            self.capture_owner_exit_liveness=liveness;self.capture_owner_released=True
            self.owner_exit_persist_error='设备组已退出，但设备所有权记录未能清理：'+str(exc)
            self.capture_registry_cleanup_pending=True
            return self._persist_owner_exit_proof()
        self.capture_owner_exit_run=run;self.capture_owner_exit_record=record
        self.capture_owner_exit_liveness=dict(liveness_verified=True,registry_cleanup_confirmed=True,group_absent=True)
        self.capture_owner_run=None;self.capture_owner_released=True
        self.capture_registry_cleanup_pending=False
        return self._persist_owner_exit_proof()

    def reap(self):
        self.worker.wait(timeout=2)
        if not self.owner_finished():return False
        try:
            try:self.worker.stdin.close()
            except BrokenPipeError:pass
            self.log.close()
        finally:
            self.worker=None
            self.log=None
            self.reference_reaped()
        return True

    def poll(self):
        tick=getattr(self.voice,'tick',None)
        if callable(tick):tick()
        if not self.worker:
            return
        self.poll_stop()
        if getattr(self,'discard_requested',False):
            self.poll_discard();return
        if self.state=='finalizing' and time.monotonic()>getattr(self,'finalize_deadline',float('inf')) and self.worker.poll() is None:
            if self.terminate_capture_owner(signal.SIGKILL):
                try:self.worker.wait(timeout=5)
                except subprocess.TimeoutExpired:pass
                try:atomic_json(self.path/'controller_error.json',dict(error='finalization timed out after the verified owner group exceeded its 30-minute bound',at=time.time()))
                except OSError:pass
            else:
                privacy=getattr(getattr(self,'dashboard',None),'privacy',None)
                if privacy is not None:privacy.fault('文件收尾超过有界等待时间，但设备进程所有权无法确认，未发送强制结束信号')
                self.last_help=['文件收尾超过有界等待时间；设备进程所有权未能核验，隐私遮挡保持，请联系维护人员。']
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
            # A reaped adapter alone is not proof its hardware descendants left.
            if not self.owner_finished():return
            summary_path=self.path/'capture_summary.json'
            summary_parse_error=None
            try:
                summary=json.loads(summary_path.read_text(encoding='utf-8'))
                if not isinstance(summary,dict):raise ValueError('capture summary is not an object')
            except (OSError,ValueError,TypeError,UnicodeError) as error:
                summary_parse_error='worker exited without a readable final summary: '+str(error)
                if summary_path.exists():
                    try:
                        archive=self.path/('capture_summary.invalid.'+str(time.time_ns())+'.bin')
                        archive.write_bytes(summary_path.read_bytes())
                    except OSError:pass
                summary=dict(status='error',errors=[summary_parse_error],time=time.time())
                try:atomic_json(summary_path,summary)
                except OSError:self.owner_exit_persist_error='采集摘要损坏且错误摘要未能写入磁盘'
            owner_exit_recovered=getattr(self,'owner_exit_recovered',False)
            if owner_exit_recovered:
                recovery_error='已确认采集进程组退出，但未取得有效原生STOP收尾凭据；本段保留并标记失败。'
                summary['status']='error';summary['qc_state']='failed'
                summary['errors']=list(dict.fromkeys(list(summary.get('errors',[]))+[recovery_error]))
                try:atomic_json(summary_path,summary)
                except OSError:self.owner_exit_persist_error='停止失败状态未能写入采集摘要'
            owner_error=getattr(self,'owner_command_error',None)
            durability=summary.get('storage_durability')
            pending=(not owner_exit_recovered and summary.get('status')=='pending_qc'
                     and isinstance(durability,dict) and durability.get('completed') is True)
            ok=(summary.get('status')=='success' or pending) and self.worker.returncode==0 and not owner_error
            if owner_exit_recovered:ok=False
            qualification_errors=[]
            if summary.get('status')=='success':
                qualification_errors=_full_capture_qualification_errors(self.path,summary)
                if qualification_errors:
                    summary['status']='qc_error'
                    summary['qc_state']='failed'
                    summary['qualification_errors']=qualification_errors
                    summary['errors']=list(summary.get('errors',[]))+qualification_errors
                    summary['errors']=list(dict.fromkeys(summary['errors']))
                    atomic_json(summary_path,summary)
                    ok=False
            # Detailed findings remain in the report and web review. Do not
            # postpone ledger commit behind a long queue of spoken warnings.
            retry=getattr(self,'retry_requested',False)
            outcome='error' if owner_exit_recovered or summary_parse_error else 'qc_error' if qualification_errors else 'error' if not ok and summary.get('status')!='cancelled' else 'failed' if retry else 'success' if ok else 'interrupted'
            if pending and ok and not retry:outcome='pending_qc'
            if owner_error:outcome='error'
            if self.quit and not getattr(self,'keep_requested',False):outcome='interrupted'
            meta_path=self.path/'meta.yaml'
            try:
                if meta_path.exists():
                    meta=yaml.safe_load(meta_path.read_text(encoding='utf-8')) or {}
                    if not isinstance(meta,dict):raise ValueError('meta.yaml is not an object')
                else:
                    source=self.path/'meta.json'
                    raw=json.loads(source.read_text(encoding='utf-8')) if source.exists() else {}
                    meta=yaml.safe_load((PROJECT/'configs/meta.yaml').read_text(encoding='utf-8')) or {}
                    meta.update(session_dir=str(self.path),environment=raw.get('reference_image','').replace('configs/environments/','',1),pi=raw)
            except (OSError,ValueError,TypeError,yaml.YAMLError) as error:
                if not owner_exit_recovered:raise
                try:
                    if meta_path.exists():
                        (self.path/('meta.invalid.'+str(time.time_ns())+'.bin')).write_bytes(meta_path.read_bytes())
                except OSError:pass
                meta=dict(session_dir=str(self.path),status='error',retention_state='kept_failed',
                          reference_state='uncollected',upload_state='excluded',
                          controller_metadata_error=str(error)[:200])
            meta.update(self.reference_session_metadata())
            meta.update(collection_mode=COLLECTION_MODE,collection_context=COLLECTION_CONTEXT)
            meta['status']=outcome
            meta['retention_state']='kept_pending_qc' if outcome=='pending_qc' else 'kept' if outcome=='success' else 'kept_failed' if outcome in ('error','qc_error') else outcome
            meta['retained_at_unix']=time.time() if outcome in ('success','pending_qc','error','qc_error') else None
            meta['reference_state']='pending_qc' if outcome=='pending_qc' else 'collected' if outcome=='success' else 'uncollected'
            meta['upload_state']='pending_qc' if outcome=='pending_qc' else 'retained_for_upload' if outcome=='success' else 'excluded'
            try:ledger.atomic_yaml(meta_path,meta)
            except (OSError,ValueError) as error:
                if not owner_exit_recovered:raise
                self.last_help.append('错误metadata未能写入磁盘：'+str(error)[:160])
            if retry:
                try:atomic_json(self.path/'retry.json',dict(status=outcome,reason='operator requested retry',reference_image=str(self.image),at=time.time()))
                except OSError:
                    if not owner_exit_recovered:raise
                    self.last_help.append('重采原因未能写入磁盘。')
            if not self.smoke:
                try:ledger_result=ledger.mark(self.image,self.path,outcome)
                except (OSError,ValueError) as error:
                    if not owner_exit_recovered:raise
                    ledger_result=False;self.last_help.append('失败台账未能更新：'+str(error)[:160])
                if outcome in ('success','pending_qc') and ledger_result is False:
                    reason='完成资格凭据在账本提交时已失效。'
                    outcome='qc_error';ok=False
                    summary['status']='qc_error';summary['qc_state']='failed'
                    summary['errors']=list(dict.fromkeys(list(summary.get('errors',[]))+[reason]))
                    atomic_json(summary_path,summary)
                    meta.update(status=outcome,retention_state='kept_failed',reference_state='uncollected',upload_state='excluded')
                    ledger.atomic_yaml(meta_path,meta)
                    ledger.mark(self.image,self.path,'qc_error')
            if outcome=='success':
                self.success_count+=1
                self.retained_created_ids=getattr(self,'retained_created_ids',set());self.retained_created_ids.add(self.path.name)
            elif outcome!='pending_qc' and not self.notified_error and not retry and summary.get('status')!='cancelled':
                self.error_voice(summary.get('errors',['采集进程异常退出，请检查日志']))
            self.exit_code=0 if (ok or summary.get('status')=='cancelled') and not owner_error else 1
            self._reference_reap_outcome=outcome
            if not self.reap():return
            self.retry_requested=False
            self.last_review_path=self.path
            self.last_completed={'session':self.path.name,'outcome':outcome,'qc_level':summary.get('qc_level')}
            if self.quit:return
            if self.smoke:
                self.quit=True;self.state='idle';self.state_write();return
            if retry:
                self.state='idle';self.state_write()
                self.voice.say('本条已标记失败并保留。请手动点击重新准备本段。');return
            if not ok:
                self.state='failed_wait';self.state_write()
                self.voice.say('本条未标记已用。请点击重采本段，或重复播报错误排查提示。')
                return
            self.state='idle';self.state_write()
            if pending:
                self.voice.say('本段已保存，等待主机质检。请手动点击准备下一段。');return
            hint='本段存在质量提示，请在网页查看详情。' if summary.get('warnings') or summary.get('qc_level')=='WARN' else ''
            self.voice.say('本段已保存并完成检验。'+hint+'可以回看录像，或手动点击准备下一段。')

    def key(self,key):
        if self.state in ('usb_importing','usb_import_recovery') and key!='q':return False
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
        privacy_required=bool(getattr(getattr(self,'dashboard',None),'privacy',None)
            and self.dashboard.privacy.snapshot().get('required'))
        if key=='a' and privacy_required and self.state in ('recording','countdown','finalizing','error','failed_wait'):
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
                else:self.voice.say((self.last_help[0] if getattr(self,'last_help',[]) else '必需采集设备未全部通过在线检查。请检查连接后点击重新扫描。'))
            elif self.state in ('idle','failed_wait'):
                self.prepare()
            elif self.state=='exhausted':
                try:self.reference_refresh(force=True)
                except (OSError,ValueError,RecursionError) as error:
                    self.reference_prepare_failed(error);return
                self.prepare()
            elif self.state in ('preparing','ready'):
                result=self.begin()
                if self.keyboard:self.keyboard.discard()
                return result
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
    lock=(CODE_BASE/'runtime'/'launcher.lock').open('w')
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit('Another instance is already running')
    # A previous boot cannot leave its controller or device process group
    # alive. Reconcile only the independently witnessed run, while this
    # controller owns the launcher lock; same-boot owners remain fail-closed.
    try:
        recovered=Registry(CODE_BASE/'runtime').reconcile_startup(lock)
        if recovered and not recovered.get('preserved'):
            print('STARTUP OWNER RECOVERY '+json.dumps(recovered,ensure_ascii=False),flush=True)
    except (RegistryError,OSError) as exc:
        # Keep the service/UI available to report the blocked registry state.
        # The next reservation still performs strict registry validation.
        print('STARTUP OWNER RECOVERY BLOCKED '+str(exc),flush=True)
    (BASE/'runtime'/'launcher.pid').write_text(str(os.getpid()))
    try:
        controller=Controller(args.smoke,args.image,args.session_dir,args.console)
        controller.launcher_lock=lock
    except CloudVoiceError as exc:
        print('CLOUD VOICE SAFETY STOP: '+str(exc),flush=True)
        sys.exit(78)
    except Exception as exc:
        traceback.print_exc()
        try:Speaker().say('启动失败。'+str(exc))
        except Exception:pass
        sys.exit(1)
    result=controller.run()
    if getattr(controller,'switch_requested',None):
        from collection_profiles import activate
        activate(CODE_BASE,controller.switch_requested,PROFILE)
        # All owners, previews and HTTP workers have closed before re-importing
        # the new profile. Keep systemd's main process and global lock.
        lock.close()
        os.environ['ORANGEPI_EARLY_WEB']='1'
        os.execv(sys.executable,[sys.executable,'-u',str(SRC/'control/launcher.py')]+sys.argv[1:])
    sys.exit(result)
