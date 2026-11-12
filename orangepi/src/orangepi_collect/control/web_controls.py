"""Dashboard/controller bridge. Hardware ownership and actions stay on main thread."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from project_paths import BASE, SRC, devices
from scan_registry import spawn_owned, RegistryError
from scan_process import run_scan, ScanProcessError
from capture_session_state import active_session
from dashboard import Dashboard, SAFE, actions_for, impedance_presentation
import recording_review
from eeg_settings import PRESETS
from wrist_calibration import SLOTS
from emg_calibration import SLOTS as EMG_SLOTS
import wrist_camera_settings as wrist_settings


class WebControls:
    def impedance_command_pending(self, state=None):
        pending=getattr(self,'_impedance_command_pending',None)
        if not pending:return False
        if pending['worker'] is not getattr(self,'worker',None):
            self._impedance_command_pending=None
            return False
        if state is None:state=self.worker_status().get('eeg_impedance') or {}
        if (state.get('ack_request_id')==pending['request_id']
                and (pending['epoch'] is None or state.get('ack_measurement_epoch')==pending['epoch']==state.get('measurement_epoch'))):
            self._impedance_command_pending=None
            return False
        return True

    def remember_pending_owner(self,error):
        self.scan_cleanup_pending=True
        run=getattr(error,'run',None);child=getattr(error,'child',None)
        self.pending_camera_owner=(run,child)
        self.owner_reconcile_pending=not run
        if run:
            self.last_help=['设备清理尚未确认：'+str(error)]
        else:
            self.last_help=['上次设备所有权恢复尚未确认；设备状态未知，已保持采集锁定。请重试恢复或联系维护人员。']

    def retry_pending_owner(self,force=False):
        if not getattr(self,'scan_cleanup_pending',False):return True
        now=time.monotonic()
        if not force and now-getattr(self,'last_owner_retry',0)<1:return False
        self.last_owner_retry=now
        run,child=getattr(self,'pending_camera_owner',(None,None))
        if not run:
            if not getattr(self,'owner_reconcile_pending',False):return False
            try:result=self.camera_registry.reconcile_startup(getattr(self,'launcher_lock',None))
            except (RegistryError,OSError) as error:
                self.last_help=['设备所有权仍未恢复，保持采集锁定：'+str(error)]
                return False
            if result and result.get('preserved'):return False
            self.owner_reconcile_pending=False
            self.pending_camera_owner=None;self.scan_cleanup_pending=False
            self.last_help=[]
            return True
        try:
            if child is not None:child.poll()
            self.camera_registry.finish(run)
        except (RegistryError,OSError):return False
        self.pending_camera_owner=None;self.scan_cleanup_pending=False;self.owner_reconcile_pending=False
        self.last_help=[]
        return True

    def audio_status(self):
        # Speaker.snapshot is an in-memory bounded copy, never a pactl/BlueZ
        # query. Audio diagnostics cannot change collection state or actions.
        try:
            snapshot=getattr(self.voice,'snapshot',None)
            if not callable(snapshot):return {}
            value=snapshot()
        except Exception:return {'warning':'音频状态暂不可用'}
        if not isinstance(value,dict):return {'warning':'音频状态暂不可用'}
        result={}
        for key,limit in (('warning',160),('current_sink',255),('last_outcome',32),('volume_sink',255),('volume_error',64)):
            text=value.get(key)
            if isinstance(text,str):result[key]=''.join(c for c in text[:limit] if ord(c)>=32 and ord(c)!=127)
        if value.get('output') in ('headphones','speaker','none','disabled'):result['output']=value['output']
        if type(value.get('speaking')) is bool:result['speaking']=value['speaking']
        for key,maximum in (('queue_size',8),('generation',9007199254740991),('volume_percent',150),('volume_generation',9007199254740991)):
            number=value.get(key)
            if type(number) is int and 0<=number<=maximum:result[key]=number
        for key in ('volume_requested','volume_applied'):
            number=value.get(key)
            if type(number) is int and 0<=number<=120:result[key]=number
        if value.get('volume_state') in ('unknown','pending','applying','reading_back','applied','failed','unavailable','disabled'):
            result['volume_state']=value['volume_state']
        return result

    def countdown_voice(self,word):
        # Short digits should expire before a later countdown can use them.
        # Signature binding preserves old one-argument voice test doubles.
        from inspect import signature
        # Validated, prerecorded digit clips avoid cloud synthesis in a 1s tick.
        digit={'五':'5','四':'4','三':'3','二':'2','一':'1'}.get(word)
        assets=getattr(self.voice,'assets',None);play=getattr(self.voice,'play',None)
        if digit and assets and callable(play):
            path=Path(assets)/('char_'+digit+'.wav')
            if path.is_file():play(str(path),ttl=.8);return
        say=self.voice.say
        try:signature(say).bind(word,ttl=.8)
        except (TypeError,ValueError):say(word)
        else:say(word,ttl=.8)

    def headband_calibration_info(self,worker):
        info=worker.get('headband_calibration')
        if isinstance(info,dict):return info
        root=getattr(self,'path',None)
        if root:
            try:
                value=json.loads((root/'ego_headband/calibration_status.json').read_text())
                if isinstance(value,dict):return value
            except (OSError,ValueError):pass
        return {}

    def save_wrist_settings(self, value, reports):
        try:
            wrist_settings.save(BASE,value)
        except (ValueError,OSError) as save_error:
            rollback_error=None
            try:rollback=wrist_settings.restore_devices(reports)
            except (ValueError,OSError) as exc:
                rollback_error=str(exc);rollback=getattr(exc,'reports',None) or {}
            for slot,report in reports.items():
                report['save_error']=str(save_error)
                report['error']='设置未保存：'+str(save_error)
                report['applied']=False
                restored=rollback.get(slot)
                if restored:
                    report['rollback']=restored
                    report['rolled_back']=bool(restored.get('applied'))
                    if restored.get('rollback_skipped'):report['rollback_skipped']=True
                    else:
                        report['actual_allow_auto_exposure_fps_change']=restored.get('actual_allow_auto_exposure_fps_change')
                        report['time']=restored.get('time',time.time())
                        if not report['rolled_back']:
                            report['rollback_error']=restored.get('error') or rollback_error or '恢复原设置未通过读回验证'
                elif not report.get('pending'):
                    report['rolled_back']=False;report['rollback_error']=rollback_error or '没有原设置恢复结果'
            message='腕部相机设置未保存：'+str(save_error)
            if rollback_error:message+='；恢复原设置失败：'+rollback_error
            error=ValueError(message);error.reports=reports
            raise error from save_error

    def wrist_settings_available(self):
        if self.state not in SAFE:return False
        try:
            with wrist_settings.preference_lock(BASE):
                if self.state=='ready' and getattr(self,'worker',None):return True
                return wrist_settings.capture_available(BASE)
        except (ValueError,OSError):return False

    def wrist_settings_status(self):
        status=dict(preference=None,devices={},can_change=self.wrist_settings_available())
        try:status['preference']=wrist_settings.load(BASE)[wrist_settings.KEY]
        except (ValueError,OSError,KeyError) as exc:status['error']=str(exc)
        recording=bool(getattr(self,'worker',None))
        root=getattr(self,'path',None) if recording else getattr(self,'preview_dir',None)
        if root:
            for slot in SLOTS:
                path=root/slot/'camera_controls.json' if recording else root/('wrist-controls-'+slot+'.json')
                try:
                    report=json.loads(path.read_text())
                    if isinstance(report,dict):status['devices'][slot]=report
                except (OSError,ValueError):pass
        status['report_source']='capture' if recording else 'preview'
        if not status['devices'] and not recording:
            status['devices']=getattr(self,'wrist_camera_last_reports',{})
            if status['devices']:status['report_source']='setting'
        status['last_change']=getattr(self,'wrist_camera_last_change',None)
        return status

    def start_dashboard(self):
        settings=devices().get('web_ui',{})
        if not settings.get('enabled',False) or self.smoke:return
        self.preview_dir=Path(tempfile.mkdtemp(prefix='orangepi-preview-',dir='/dev/shm'))
        os.environ['ORANGEPI_PREVIEW_DIR']=str(self.preview_dir)
        self.dashboard=Dashboard(BASE,self.preview_dir,settings,
            reference_catalog=getattr(self,'reference_catalog',None),
            reference_credentials=getattr(self,'reference_credentials',None),
            reference_completed_paths=getattr(self,'reference_completed_paths',None),
            reference_access=getattr(self,'reference_access',None),
            recording_store=getattr(self,'recording_store',None),
            reference_states=getattr(self,'reference_history_states',None),
            usb_task_importer=getattr(self,'usb_task_importer',None))
        from storage_paths import session_root
        self.last_review_path=recording_review.latest_recording(getattr(self,'session_dir',None) or session_root())
        self.voice.on_prompt=self.voice_prompt_changed
        self.voice.skip_requested=self.voice_skip_requested
        self.ui_publish()

    def voice_prompt_changed(self,active):
        dashboard=getattr(self,'dashboard',None)
        if dashboard is None:return
        events=getattr(dashboard,'events',None)
        if events:
            now=time.monotonic();previous=getattr(self,'_diagnostic_tick',now);self._diagnostic_tick=now
            lag=max(0,(now-previous)*1000)
            if lag>1000:events.emit('loop_lag',lag_ms=lag)
        # Speaker.tick dispatches this on the controller thread. Keep it safe
        # and limited to the dashboard's own thread-safe prompting flag.
        dashboard.set_prompting(active)

    def voice_skip_requested(self):
        # Only validated main-thread actions interrupt a generation. Looking at
        # pending jobs here would cancel speech for stale or volume-only jobs.
        return False

    def ui_publish(self,force=False):
        dashboard=getattr(self,'dashboard',None)
        if dashboard is None:return
        privacy=getattr(dashboard,'privacy',None)
        if privacy is not None and privacy.committed and getattr(self,'path',None):
            try:
                proof=json.loads((self.path/'capture_complete.json').read_text())
                privacy.confirm_stopped(proof)
            except (OSError,ValueError,TypeError):pass
        now=time.monotonic()
        revision=getattr(self,'ui_revision',0)
        if not force and revision==getattr(self,'ui_last_revision',None) and now-getattr(self,'ui_last_publish',0)<.4:return
        self.ui_last_revision=revision;self.ui_last_publish=now
        # self.path can still refer to a previous failed/reviewed session after
        # its worker was reaped. Do not present that old error as a live worker.
        try:worker=self.worker_status() if getattr(self,'worker',None) is not None else dict(state='absent',recording_active=False)
        except (OSError,ValueError):worker={}
        if (getattr(self,'owner_exit_recovered',False)
                and (getattr(self,'worker',None) is None or self.worker.poll() is not None)):
            worker=dict(worker or {},state='error',recording_active=False,
                        errors=list(getattr(self,'last_help',[])))
        cfg=getattr(self,'web_config',{})
        keyboard=getattr(self,'keyboard',None)
        if now-getattr(self,'ui_last_disk',0)>5:
            self.ui_last_disk=now
            try:
                from storage_paths import config
                usage=shutil.disk_usage(config()['mount_point'])
                self.ui_storage={'free_gb':round(usage.free/1024**3,1),'total_gb':round(usage.total/1024**3,1)}
            except (OSError,KeyError,ValueError):self.ui_storage={}
        storage=getattr(self,'ui_storage',{})
        if self.path and self.state in ('confirm_stop','finalizing','error','failed_wait') and (self.path/'stop_gate.json').exists():self.last_review_path=self.path
        review=getattr(self,'last_review_path',None)
        wizard=getattr(self,'wrist_wizard',None)
        preview=getattr(self,'preview_worker',None)
        assignment=None
        sources=getattr(self,'preview_sources',{})
        if (wizard is not None and self.state.startswith('wrist_') and preview is not None
                and preview.poll() is None and set(sources)==set(SLOTS)
                and set(sources.values())==wizard.paths):
            assignment=dict(session_id=wizard.session_id,
                preview_id=getattr(self,'preview_snapshot_id',None),sources=dict(sources))
        emg_assignment=None
        emg_wizard=getattr(self,'emg_wizard',None)
        emg_sources=getattr(self,'emg_preview_sources',{})
        if (emg_wizard is not None and self.state=='emg_manual' and preview is not None
                and preview.poll() is None and set(emg_sources)==set(EMG_SLOTS)
                and set(emg_sources.values())==emg_wizard.paths):
            emg_assignment=dict(session_id=emg_wizard.session_id,
                preview_id=getattr(self,'preview_snapshot_id',None),sources=dict(emg_sources))
        emg_status=cfg.get('emg_calibration_status',{})
        emg_roles={slot:item.get('discovery',{}).get('serial','')
                   for slot,item in cfg.get('emg',{}).items()} if emg_status.get('valid') else {}
        calibration=self.headband_calibration_info(worker)
        notices=list(getattr(self,'last_help',[]))
        for warning in (getattr(self,'reference_error',None),getattr(self,'reference_access_error',None)):
            if warning and warning not in notices:notices.append(warning)
        audio=self.audio_status()
        audio_warning=audio.get('warning','')
        if audio_warning:
            notices.append('音频提示：'+audio_warning[:160])
        if calibration.get('warnings'):
            notices.append('头环标定文件的适用性待核对，请查看本段标定记录。')
        from project_paths import COLLECTION_MODE,COLLECTION_CONTEXT
        impedance=impedance_presentation(self.state, self.selected_devices, worker,
            getattr(self,'worker',None) is not None and self.worker.poll() is None)
        if self.impedance_command_pending(worker.get('eeg_impedance') or {}):
            impedance.update(pending=True,can_record=False,can_review=False)
        if impedance.get('required') is True:
            from impedance_quality import review_eligibility
            armed=self.state!='ready' or bool(privacy and privacy.snapshot().get('required'))
            impedance.update(review_eligibility(impedance,capture_armed=armed))
            events=getattr(dashboard,'events',None)
            signature=(impedance.get('measurement_epoch'),impedance.get('phase'),impedance.get('can_review'),
                tuple(impedance.get('reason_codes') or ()),impedance.get('valid_count'),impedance.get('enabled_count'))
            if events and signature!=getattr(self,'_diagnostic_impedance_signature',None):
                self._diagnostic_impedance_signature=signature
                events.emit('impedance_gate',measurement_epoch=impedance.get('measurement_epoch'),
                    phase=impedance.get('phase'),reason=(impedance.get('reason_codes') or ['ok'])[0],
                    enabled_count=impedance.get('enabled_count'),valid_count=impedance.get('valid_count'),
                    recovered=impedance.get('normal_frame_confirmed') is True,persisted=impedance.get('completed_persisted') is True,
                    ok=impedance.get('can_review') is True)
        dashboard.publish(dict(collection_mode=COLLECTION_MODE,collection_context=COLLECTION_CONTEXT,mode_switch_target=getattr(self,'mode_switch_target',None),state=self.state,revision=getattr(self,'ui_revision',0),audio=audio,
            countdown=getattr(self,'countdown_status',None) if self.state=='countdown' else None,
            collector_id=getattr(self,'collector_id',''),preparation_cancelled_at=getattr(self,'preparation_cancelled_at',None),
            task_list=getattr(self,'reference_task_state',lambda:{})(),reference_image_state=getattr(self,'reference_image_state',lambda:{})(),
            image=str(self.image) if self.image else None,image_name=getattr(self,'reference_image_title',lambda:self.image.stem if self.image else '')(),session=active_session(self),
            success_count=self.success_count,devices=getattr(self,'last_scan',[]),
            wrist_calibration=cfg.get('wrist_calibration_status',{}),
            wrist_assignment=assignment,preview_id=getattr(self,'preview_snapshot_id',None),
            emg_serial_roles=emg_roles,emg_calibration=emg_status,emg_assignment=emg_assignment,
            emg_pairing=getattr(self,'emg_pairing_status',lambda:None)(),
            headband_calibration=calibration,
            qc_execution=__import__('qc_deferred').load(BASE),
            eeg_config=cfg.get('eeg',{}),eeg_filter_presets=PRESETS,
            wrist_camera_settings=self.wrist_settings_status(),
            selected_devices=self.selected_devices,worker=worker,
            eeg_impedance=impedance,
            last_voice=getattr(self.voice,'last_text',''),errors=notices,
            recording_since=getattr(self,'recording_since',None),
            keyboard_names=getattr(keyboard,'names',[]),storage=storage,
            review_session=str(review) if review and self.state not in ('recording','countdown') else None,
            last_completed=getattr(self,'last_completed',None),
            scan_ok=getattr(self,'scan_ok',False),
            required_devices=(getattr(self,'required_device_contract',{}) or {}).get('required_devices',[]),
            required_devices_missing=getattr(self,'required_devices_missing',[]),
            controller_time=time.time()))

    def stop_preview(self,clear=True):
        worker=getattr(self,'preview_worker',None)
        if worker:
            if worker.poll() is None:
                worker.terminate()
                try:worker.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    worker.kill()
                    try:worker.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self.last_help=['预览进程仍在退出，设备尚未释放'];return False
            worker.stdin.close()
            run=getattr(self,'preview_owner_run',None)
            if run:
                try:self.camera_registry.finish(run)
                except (RegistryError,OSError) as exc:
                    # Keep both handles so the next bounded idle pass can retry
                    # after a late descendant actually exits.
                    self.last_help=['预览设备尚未完全退出：'+str(exc)]
                    return False
                self.preview_owner_run=None
            self.preview_worker=None
        self.preview_snapshot_id=None;self.preview_sources={};self.emg_preview_sources={}
        if clear:
            for p in getattr(self,'preview_dir',Path('/nonexistent')).glob('*'):
                try:p.unlink()
                except OSError:pass
        return True

    def ensure_preview(self):
        if not getattr(self,'dashboard',None) or self.worker:return
        privacy=self.dashboard.privacy.snapshot()
        if privacy.get('required') is not False or privacy.get('stop_confirmed') is not True:return
        if not self.retry_pending_owner():return
        if self.state == 'emg_pairing' and not getattr(self,'emg_pairing_preview_allowed',lambda:False)():return
        if self.state not in ('device_confirm','idle','failed_wait','exhausted') and not self.state.startswith(('wrist_','emg_')):return
        worker=getattr(self,'preview_worker',None)
        if worker and worker.poll() is None:return
        try:
            with wrist_settings.preference_lock(BASE):
                if not wrist_settings.capture_available(BASE):return
        except (ValueError,OSError):return
        now=time.monotonic()
        if now-getattr(self,'last_preview_start',0)<5:return
        self.last_preview_start=now
        if self.stop_preview() is False:return
        cfg=devices();self.web_config=cfg
        self.preview_sources={slot:cfg['wrist_cameras'][slot]['device']
            for slot in SLOTS if slot in cfg.get('wrist_cameras',{})}
        self.emg_preview_sources={slot:cfg['emg'][slot]['port']
            for slot in EMG_SLOTS if slot in cfg.get('emg',{})}
        self.preview_snapshot_id=uuid.uuid4().hex
        cfg['_preview_id']=self.preview_snapshot_id
        cfg['_exclusive_preview']=self.state in ('emg_manual','emg_pairing')
        try:
            self.preview_worker,self.preview_owner_run=spawn_owned(self.camera_registry,'preview',[str(BASE/'.venvs/hardware/bin/python'),'-u',str(SRC/'acquisition/preview_worker.py')],
                stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,text=True)
        except (RegistryError,OSError) as exc:
            self.last_help=[str(exc)];return
        try:self.preview_worker.stdin.write(json.dumps(cfg));self.preview_worker.stdin.flush();self.preview_worker.stdin.close()
        except BrokenPipeError:pass

    def release_prepared(self):
        if not self.worker:return
        if self.state!='ready':raise ValueError('当前不可更改设备')
        if self.worker_status().get('recording_active'):raise ValueError('正在录制，不可更改设备')
        self.command('STOP');self.worker.wait(timeout=50)
        # No cancellation is recorded until the entire owner group is gone.
        if not self.reap():raise ValueError('设备进程尚未全部退出，准备未解除')
        if self.path:
            (self.path/'ui_preparation_cancelled.json').write_text(json.dumps({'reason':'operator changed setup before recording','time':time.time()}))
        self.state='idle';self.state_write()

    def abandon_preparation(self):
        from dashboard import preparation_state
        if not preparation_state(self.state):raise ValueError('正式采集开始后请先停止并处理本段结果')
        privacy=getattr(getattr(self,'dashboard',None),'privacy',None)
        if privacy is not None and privacy.snapshot().get('required'):raise ValueError('采集状态正在同步，请先完成停止确认')
        status=self.worker_status() if getattr(self,'worker',None) else {}
        path=getattr(self,'path',None)
        if status.get('recording_active') or (path and (path/'start_gate.json').exists()):raise ValueError('本段已正式开录，不能按准备放弃')
        if getattr(self,'worker',None):
            self.command('STOP');self.worker.wait(timeout=50)
            if not self.reap():raise ValueError('设备进程尚未全部退出，文件未清理')
            if path:
                from recording_store import atomic_json
                atomic_json(path/'ui_preparation_cancelled.json',dict(reason='operator_abandoned_before_start',time=time.time()))
            store=getattr(self,'recording_store',None)
            if store is not None and path and path.is_dir():store.abandon_preparation(path)
        if self.stop_preview() is False:raise ValueError('预览设备尚未全部退出')
        close=getattr(self,'close_emg_pairing',None)
        if callable(close):close()
        self.wrist_wizard=self.emg_wizard=None;self._emg_pairing_spoken_phase=None
        self.reference_release_pin();self._reference_active=None;self._reference_retry=None
        self._reference_cli_image=None;self._reference_cli_force=False;self.selected_image=None
        self._reference_manual_id=None
        self.path=None;self.image=None;self.selected_devices=None
        self.last_completed=None;self.last_help=[];self.owner_command_error=None
        self.countdown_status=None;self._worker_status_cache=None
        self.reference_refresh(force=True);self.preparation_cancelled_at=time.time()
        self.scan_devices(announce=True,quiet=True)
        self.state='device_confirm';self.state_write()

    def delete_recording(self,value):
        if self.state not in ('device_confirm','idle','failed_wait','exhausted') or getattr(self,'worker',None):raise ValueError('请先结束或放弃当前准备，再管理采集条目')
        if self.dashboard.privacy.snapshot().get('required'):raise ValueError('采集状态未确认停止')
        result=self.recording_store.delete(value['session_id'],value['token'])
        created=getattr(self,'retained_created_ids',set())
        if value['session_id'] in created:created.discard(value['session_id']);self.success_count=max(0,self.success_count-1)
        if (getattr(self,'last_completed',None) or {}).get('session')==value['session_id']:self.last_completed=None
        self.reference_refresh(force=True)
        if self.state=='exhausted' and self.images:self.state='idle'
        if getattr(self,'path',None) and self.path.name==value['session_id']:self.path=None;self.image=None
        self.last_review_path=recording_review.latest_recording(self.recording_store.roots()[0])
        self.state_write()
        return result

    def retain_error_recording(self,value):
        if self.state not in ('device_confirm','idle','failed_wait','exhausted') or getattr(self,'worker',None):raise ValueError('请先结束当前准备或采集')
        if self.dashboard.privacy.snapshot().get('required'):raise ValueError('采集状态未确认停止')
        result=self.recording_store.retain_error(value['session_id'],value['token'])
        self.reference_refresh(force=True);self.state_write();return result

    def shutdown_device(self):
        if self.state not in SAFE or getattr(self,'worker',None):raise ValueError('请先结束当前准备或采集')
        if self.dashboard.privacy.snapshot().get('required'):raise ValueError('采集状态未确认停止')
        # Reply to the page first, then power off; the helper runs as root via
        # the narrow sudo rule installed by the service installer.
        threading.Timer(2.0,self._execute_shutdown).start()
        return {'shutdown':'scheduled'}

    def _execute_shutdown(self):
        try:subprocess.run(['sudo','-n','/usr/local/lib/headring-power/shutdown'],timeout=30,check=False)
        except OSError:pass

    def dashboard_tick(self):
        reference_tick=getattr(self,'reference_tick',None)
        if callable(reference_tick):reference_tick()
        dashboard=getattr(self,'dashboard',None)
        if dashboard is None:return
        tick=getattr(self.voice,'tick',None)
        if callable(tick):tick()
        self.ui_publish()
        job=dashboard.take()
        if job:
            error=None
            try:
                impedance = self.worker_status().get('eeg_impedance', {}) if 'eeg' in (getattr(self,'selected_devices',None) or ()) else dict(required=False)
                allowed=list(actions_for(self.state,impedance))
                privacy=dashboard.privacy.snapshot()
                already_stopped=(job['action']=='stop' and privacy.get('phase')=='idle'
                    and dashboard.privacy.target_matches(job.get('privacy_target'),allow_stopped=True))
                stop_bound=(job['action']=='stop' and dashboard.privacy.target_matches(
                    job.get('privacy_target'),allow_same_session=True))
                if already_stopped:allowed.append('stop')
                if privacy.get('required') and privacy.get('stop_confirmed') is not True and stop_bound:
                    allowed.append('stop')
                if job['action']=='stop' and not stop_bound and not already_stopped:
                    raise ValueError('采集会话已变化，请刷新后重试')
                if getattr(self,'reference_can_switch_list',getattr(self,'reference_can_change',lambda:False))():allowed.append('reference_list')
                mode_action=job['action']=='collection_mode'
                if mode_action:
                    from project_paths import COLLECTION_CONTEXT
                    if job.get('collection_context')!=COLLECTION_CONTEXT:raise ValueError('采集模式已变化，请刷新页面后操作')
                stale_revision=job['revision']!=getattr(self,'ui_revision',0)
                if (not mode_action and stale_revision and not stop_bound and not already_stopped) or job['action'] not in allowed:
                    raise ValueError('流程已经变化，本次点击未执行，请重新选择')
                action=job['action']
                if action not in ('volume','eeg_filter','wrist_camera_settings'):
                    interrupt=getattr(self.voice,'interrupt',None)
                    if callable(interrupt):interrupt()
                if action=='wrist_camera_settings':
                    value=job.get('value')
                    if 'value' not in job or (value is not None and type(value) is not bool):
                        raise ValueError('无效的腕部相机帧率选项')
                    if not self.wrist_settings_available():raise ValueError('设备正在采集或交接，暂不可更改腕部相机设置')
                if action in ('rescan','calibrate_wrist','calibrate_emg','eeg_filter','wrist_camera_settings'):
                    if action=='rescan' and self.scan_prohibited():raise ValueError('设备所有权或采集状态尚未释放，扫描已延后')
                    self.release_prepared()
                    if self.stop_preview() is False:raise ValueError('预览设备尚未全部退出')
                if action=='abandon':
                    self.abandon_preparation()
                elif action=='stop':
                    privacy=dashboard.privacy
                    target=job.get('privacy_target')
                    if already_stopped:pass
                    elif not privacy.target_matches(target,allow_same_session=True):
                        raise ValueError('采集会话已变化，请刷新后重试')
                    elif not privacy.snapshot().get('required'):
                        raise ValueError('当前会话已经确认停止，请刷新页面')
                    elif self.stop_before_prompt(target) is False:
                        raise ValueError(getattr(self,'owner_command_error',None) or '停止请求未能送达，请重试')
                elif action=='discard_recording':
                    if self.discard_recording() is False:
                        raise ValueError(getattr(self,'owner_command_error',None) or '设备停止尚未确认，本段尚未清理')
                elif action=='collection_mode':
                    self.switch_collection_mode(job['value'])
                elif action=='delete_recording':
                    self.delete_recording(job['value'])
                elif action=='retain_error_recording':
                    self.retain_error_recording(job['value'])
                elif action in ('select_reference','switch_reference'):
                    self.select_reference_image(job.get('value'),next_image=action=='switch_reference')
                elif action=='reference_list':
                    self.select_reference_list(job['value'])
                elif action=='usb_task_import':
                    self.start_usb_task_import(job['value'])
                elif action=='qc_mode':
                    if getattr(self,'worker',None):raise ValueError('请在新一段准备前切换质检模式')
                    __import__('qc_deferred').save(BASE,job['value']);self.state_write()
                elif action in ('impedance_start','impedance_end','impedance_review'):
                    epoch=job.get('measurement_epoch')
                    if action!='impedance_start' and (not epoch or epoch!=impedance.get('measurement_epoch')):
                        raise ValueError('阻抗测量已变化，请刷新后重试')
                    if action=='impedance_review':
                        from impedance_quality import review_eligibility
                        eligibility=review_eligibility(impedance,capture_armed=privacy.get('required') is True)
                        if not eligibility['can_review']:raise ValueError('阻抗结果暂不可确认：'+','.join(eligibility['reason_codes']))
                    command = {'impedance_start':'IMPEDANCE_START','impedance_end':'IMPEDANCE_END',
                               'impedance_review':'IMPEDANCE_REVIEW'}[action]
                    payload=dict(command=command,request_id=job.get('request_id') or uuid.uuid4().hex)
                    if action!='impedance_start':payload['measurement_epoch']=epoch
                    self._impedance_command_pending=dict(worker=getattr(self,'worker',None),request_id=payload['request_id'],epoch=epoch)
                    self.command(json.dumps(payload))
                    self.state_write()
                elif action=='wrist_camera_settings':
                    output=run_scan([str(BASE/'.venvs/hardware/bin/python'),str(SRC/'control/wrist_settings_worker.py')],
                        self.camera_registry,kind='settings',input_text=json.dumps(value),timeout=50)
                    result=json.loads(output);reports=result.get('reports') or {}
                    if not result.get('ok'):
                        error=ValueError(result.get('error') or '腕部相机设置未确认');error.reports=reports;raise error
                    self.wrist_camera_last_reports=reports
                    self.web_config=devices()
                    self.wrist_camera_last_change=dict(ok=True,error=None,time=time.time(),devices=reports)
                    self.state_write()
                elif action=='eeg_filter':
                    if job.get('value') not in PRESETS:raise ValueError('无效的脑电滤波选项')
                    from eeg_settings import apply as apply_eeg
                    path=BASE/'runtime/eeg_preferences.json';temp=path.with_suffix('.json.tmp')
                    temp.write_text(json.dumps({'filter_preset':job['value']}));temp.replace(path)
                    self.web_config=apply_eeg(getattr(self,'web_config',{}) or devices(),BASE)
                    self.state_write()
                elif action=='rescan':
                    if self.scan_devices() is False:raise ValueError(self.last_help[0])
                elif action=='calibrate_wrist':
                    if not self.selected_devices:self.selected_devices=tuple(self.scan_available)
                    self.start_wrist_calibration()
                elif action=='wrist_method':self.set_wrist_method(job['value'])
                elif action=='assign_wrist_roles':self.assign_wrist_roles(job['value'])
                elif action=='calibrate_emg':
                    if not self.selected_devices:self.selected_devices=tuple(self.scan_available)
                    self.start_emg_calibration()
                elif action=='assign_emg_roles':self.assign_emg_roles(job['value'])
                elif action=='volume':
                    setter=getattr(self.voice,'set_volume',None)
                    if callable(setter):setter(job['value'])
                    else:
                        result=subprocess.run(['pactl','set-sink-volume','@DEFAULT_SINK@',str(job['value'])+'%'],env=self.voice.env,capture_output=True,text=True,timeout=3)
                        if result.returncode:raise ValueError('音量调整失败')
                elif action=='shutdown':
                    self.shutdown_device()
                else:
                    key={'confirm_devices':'a','prepare':'a','measure':'a','start':'a','stop':'a','keep':'a',
                        'retry':'b','restart_calibration':'b','repeat':'b'}[action]
                    if self.key(key) is False:
                        raise ValueError(getattr(self,'owner_command_error',None) or '本次操作未完成，请查看当前状态')
            except self.OwnerExited as exc:
                error=str(exc)
                self.owner_command_failed(exc)
            except (ValueError,OSError,subprocess.TimeoutExpired,ScanProcessError) as exc:
                error=str(exc)
                if isinstance(exc,ScanProcessError) and exc.cleanup_pending:
                    self.remember_pending_owner(exc)
                if job['action']=='wrist_camera_settings':
                    reports=getattr(exc,'reports',None) or {}
                    if reports:self.wrist_camera_last_reports=reports
                    self.wrist_camera_last_change=dict(ok=False,error=error,time=time.time(),devices=reports)
            except Exception as exc:
                error=str(exc) or type(exc).__name__
                raise
            finally:
                try:
                    if self.keyboard:self.keyboard.discard()
                    self.ui_publish()
                except Exception as exc:
                    if error is None:error=str(exc) or type(exc).__name__
                    raise
                finally:dashboard.complete(error)
        self.ensure_preview()

    def close_dashboard(self):
        if hasattr(self,'close_emg_pairing'):self.close_emg_pairing()
        preview_stopped=self.stop_preview()
        if getattr(self,'dashboard',None):self.dashboard.close();self.dashboard=None
        if preview_stopped is not False and getattr(self,'preview_dir',None):shutil.rmtree(self.preview_dir,ignore_errors=True)
        close_references=getattr(self,'reference_close',None)
        if callable(close_references):close_references()
