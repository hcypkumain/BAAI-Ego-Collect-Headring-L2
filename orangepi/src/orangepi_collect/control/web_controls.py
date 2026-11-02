"""Dashboard/controller bridge. Hardware ownership and actions stay on main thread."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import uuid
from project_paths import BASE, SRC, devices
from dashboard import Dashboard, SAFE, actions_for, impedance_presentation
import recording_review
from eeg_settings import PRESETS
from wrist_calibration import SLOTS
from emg_calibration import SLOTS as EMG_SLOTS
import wrist_camera_settings as wrist_settings


class WebControls:
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
        for key,limit in (('warning',160),('current_sink',255),('last_outcome',32)):
            text=value.get(key)
            if isinstance(text,str):result[key]=''.join(c for c in text[:limit] if ord(c)>=32 and ord(c)!=127)
        if value.get('output') in ('headphones','speaker','none','disabled'):result['output']=value['output']
        if type(value.get('speaking')) is bool:result['speaking']=value['speaking']
        for key,maximum in (('queue_size',8),('generation',9007199254740991),('volume_percent',150)):
            number=value.get(key)
            if type(number) is int and 0<=number<=maximum:result[key]=number
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
            reference_states=getattr(self,'reference_history_states',None))
        from storage_paths import session_root
        self.last_review_path=recording_review.latest_recording(getattr(self,'session_dir',None) or session_root())
        self.voice.on_prompt=self.voice_prompt_changed
        self.voice.skip_requested=self.voice_skip_requested
        self.ui_publish()

    def voice_prompt_changed(self,active):
        dashboard=getattr(self,'dashboard',None)
        if dashboard is None:return
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
                proof=json.loads((self.path/'stop_gate.json').read_text())
                privacy.confirm_stopped(proof)
            except (OSError,ValueError,TypeError):pass
        now=time.monotonic()
        revision=getattr(self,'ui_revision',0)
        if not force and revision==getattr(self,'ui_last_revision',None) and now-getattr(self,'ui_last_publish',0)<.4:return
        self.ui_last_revision=revision;self.ui_last_publish=now
        try:worker=self.worker_status()
        except (OSError,ValueError):worker={}
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
        dashboard.publish(dict(state=self.state,revision=getattr(self,'ui_revision',0),audio=audio,
            countdown=getattr(self,'countdown_status',None) if self.state=='countdown' else None,
            collector_id=getattr(self,'collector_id',''),preparation_cancelled_at=getattr(self,'preparation_cancelled_at',None),
            task_list=getattr(self,'reference_task_state',lambda:{})(),reference_image_state=getattr(self,'reference_image_state',lambda:{})(),
            image=str(self.image) if self.image else None,image_name=getattr(self,'reference_image_title',lambda:self.image.stem if self.image else '')(),session=str(self.path) if self.path else None,
            success_count=self.success_count,devices=getattr(self,'last_scan',[]),
            wrist_calibration=cfg.get('wrist_calibration_status',{}),
            wrist_assignment=assignment,preview_id=getattr(self,'preview_snapshot_id',None),
            emg_serial_roles=emg_roles,emg_calibration=emg_status,emg_assignment=emg_assignment,
            emg_pairing=getattr(self,'emg_pairing_status',lambda:None)(),
            headband_calibration=calibration,
            eeg_config=cfg.get('eeg',{}),eeg_filter_presets=PRESETS,
            wrist_camera_settings=self.wrist_settings_status(),
            selected_devices=self.selected_devices,worker=worker,
            eeg_impedance=impedance_presentation(self.state, self.selected_devices, worker,
                getattr(self,'worker',None) is not None and self.worker.poll() is None),
            last_voice=getattr(self.voice,'last_text',''),errors=notices,
            recording_since=getattr(self,'recording_since',None),
            keyboard_names=getattr(keyboard,'names',[]),storage=storage,
            review_session=str(review) if review and self.state not in ('recording','countdown') else None,
            last_completed=getattr(self,'last_completed',None),
            controller_time=time.time()))

    def stop_preview(self,clear=True):
        worker=getattr(self,'preview_worker',None)
        if worker:
            if worker.poll() is None:
                worker.terminate()
                try:worker.wait(timeout=6)
                except subprocess.TimeoutExpired:worker.kill();worker.wait(timeout=3)
            worker.stdin.close();self.preview_worker=None
        self.preview_snapshot_id=None;self.preview_sources={};self.emg_preview_sources={}
        if clear:
            for p in getattr(self,'preview_dir',Path('/nonexistent')).glob('*'):
                try:p.unlink()
                except OSError:pass

    def ensure_preview(self):
        if not getattr(self,'dashboard',None) or self.worker:return
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
        self.stop_preview()
        cfg=devices();self.web_config=cfg
        self.preview_sources={slot:cfg['wrist_cameras'][slot]['device']
            for slot in SLOTS if slot in cfg.get('wrist_cameras',{})}
        self.emg_preview_sources={slot:cfg['emg'][slot]['port']
            for slot in EMG_SLOTS if slot in cfg.get('emg',{})}
        self.preview_snapshot_id=uuid.uuid4().hex
        cfg['_preview_id']=self.preview_snapshot_id
        cfg['_exclusive_preview']=self.state in ('emg_manual','emg_pairing')
        self.preview_worker=subprocess.Popen([str(BASE/'.venvs/ros/bin/python'),'-u',str(SRC/'acquisition/preview_worker.py')],
            stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,text=True)
        try:self.preview_worker.stdin.write(json.dumps(cfg));self.preview_worker.stdin.flush();self.preview_worker.stdin.close()
        except BrokenPipeError:pass

    def release_prepared(self):
        if not self.worker:return
        if self.state!='ready':raise ValueError('当前不可更改设备')
        if self.worker_status().get('recording_active'):raise ValueError('正在录制，不可更改设备')
        self.command('STOP');self.worker.wait(timeout=50)
        # No recording was started, so this reference is still available.
        if self.path:
            (self.path/'ui_preparation_cancelled.json').write_text(json.dumps({'reason':'operator changed setup before recording','time':time.time()}))
        self.reap();self.state='idle';self.state_write()

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
            if path:
                from recording_store import atomic_json
                atomic_json(path/'ui_preparation_cancelled.json',dict(reason='operator_abandoned_before_start',time=time.time()))
            self.reap()
            store=getattr(self,'recording_store',None)
            if store is not None and path and path.is_dir():store.abandon_preparation(path)
        self.stop_preview()
        close=getattr(self,'close_emg_pairing',None)
        if callable(close):close()
        self.wrist_wizard=self.emg_wizard=None;self._emg_pairing_spoken_phase=None
        self.reference_release_pin();self._reference_active=None;self._reference_retry=None
        self._reference_cli_image=None;self._reference_cli_force=False;self.selected_image=None
        self._reference_manual_id=None
        self.path=None;self.image=None;self.selected_devices=None
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
                if getattr(self,'reference_can_change',lambda:False)():allowed.append('reference_list')
                if job['revision']!=getattr(self,'ui_revision',0) or job['action'] not in allowed:
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
                    self.release_prepared();self.stop_preview()
                if action=='abandon':
                    self.abandon_preparation()
                elif action=='delete_recording':
                    self.delete_recording(job['value'])
                elif action=='retain_error_recording':
                    self.retain_error_recording(job['value'])
                elif action in ('select_reference','switch_reference'):
                    self.select_reference_image(job.get('value'),next_image=action=='switch_reference')
                elif action=='reference_list':
                    self.select_reference_list(job['value'])
                elif action in ('impedance_start','impedance_end','impedance_review'):
                    command = {'impedance_start':'IMPEDANCE_START','impedance_end':'IMPEDANCE_END',
                               'impedance_review':'IMPEDANCE_REVIEW'}[action]
                    self.command(json.dumps(dict(command=command,request_id=uuid.uuid4().hex)))
                    self.state_write()
                elif action=='wrist_camera_settings':
                    with wrist_settings.preference_lock(BASE),wrist_settings.capture_guard(BASE):
                        self.wrist_camera_last_reports=wrist_settings.configure_devices(devices(),value)
                        self.save_wrist_settings(value,self.wrist_camera_last_reports)
                        self.web_config=devices()
                    self.wrist_camera_last_change=dict(ok=True,error=None,time=time.time(),devices=self.wrist_camera_last_reports)
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
                else:
                    key={'confirm_devices':'a','prepare':'a','measure':'a','start':'a','stop':'a','keep':'a',
                        'retry':'b','restart_calibration':'b','repeat':'b'}[action]
                    if self.key(key) is False:
                        raise ValueError(getattr(self,'owner_command_error',None) or '本次操作未完成，请查看当前状态')
            except self.OwnerExited as exc:
                error=str(exc)
                self.owner_command_failed(exc)
            except (ValueError,OSError,subprocess.TimeoutExpired) as exc:
                error=str(exc)
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
        self.stop_preview()
        if getattr(self,'dashboard',None):self.dashboard.close();self.dashboard=None
        if getattr(self,'preview_dir',None):shutil.rmtree(self.preview_dir,ignore_errors=True)
        close_references=getattr(self,'reference_close',None)
        if callable(close_references):close_references()
