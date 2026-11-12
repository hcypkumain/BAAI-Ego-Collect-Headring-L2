import fcntl, json, os, threading, time, uuid, shutil
from pathlib import Path
import metrics, operations, broker_client, imports
class Jobs:
    def __init__(self,state,config,*,registry):
        operations.require_config(config,registry)
        self.registry=registry;self.state=Path(state); self.config=json.loads(json.dumps(config))
        self.lockfile=open(self.state/'owner.lock','a+'); os.chmod(self.state/'owner.lock',0o600)
        try: fcntl.flock(self.lockfile,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.lockfile.close(); raise
        self.guard=threading.Lock(); self.jobs={}; self.cache={}
        try:
            self.jobs=json.loads((self.state/'jobs.json').read_text())
            for job in self.jobs.values():
                if job.get('kind') in ('apply','import') and job.get('status') in ('queued','preflight','applying','verifying'): job['status']='interrupted_reconcile'
                elif job.get('status')=='running': job['status']='interrupted'
            self._save_jobs()
        except (OSError,ValueError,AttributeError): self.jobs={}
        try: self.cache=json.loads((self.state/'cache.json').read_text())
        except (OSError,ValueError): pass
        backup=self.state/'cache-v1-backup.json'
        if self.cache and not backup.exists() and any(isinstance(r,dict) and r.get('schema')!=2 for r in self.cache.values()):
            try:
                fd=os.open(backup,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                with os.fdopen(fd,'w') as out: json.dump(self.cache,out,ensure_ascii=False)
            except OSError: pass
        for row in self.cache.values():
            if isinstance(row,dict): row['online']=False
    def _save_jobs(self):
        data=dict(list(self.jobs.items())[-100:])
        tmp=self.state/'jobs.tmp'
        fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        with os.fdopen(fd,'w') as out: json.dump(data,out,ensure_ascii=False)
        os.replace(tmp,self.state/'jobs.json')
    def refresh(self,device,kind="refresh"):
        operations.require_config(self.config,self.registry)
        if kind not in ("refresh","check","plan","audit"): raise ValueError("invalid job")
        if device not in self.config['devices']: raise ValueError('invalid device')
        if not self.guard.acquire(False): raise RuntimeError('已有任务正在执行')
        jid=uuid.uuid4().hex; self.jobs[jid]={'id':jid,'device':device,'status':'running','kind':kind}
        try: self._save_jobs()
        except OSError:
            self.guard.release(); raise RuntimeError('cannot persist job')
        def run():
            try:
                row=(metrics.collect(self.config,device) if kind=="refresh" else metrics.deep_check(self.config,device) if kind=="check" else metrics.audit_history(self.config,device) if kind=="audit" else metrics.create_plan(self.config,device,self.state))
                if kind in ("check","plan","audit"):
                    field="deep_check" if kind=="check" else "history_audit" if kind=="audit" else "review_plan"
                    self.cache[device]={**self.cache.get(device,{}),field:row}
                    self.cache[device].pop("plan_error" if kind=="plan" else "check_error",None)
                    self.jobs[jid]["status"]="done"
                    return
                if row.get('identity_mismatch'):
                    old=self.cache.get(device,{})
                    self.cache[device]={**old,'online':False,'identity_mismatch':True,'checked_at':row['checked_at']}
                else:
                    for field in ("deep_check","review_plan","history_audit","usb_scan","import_plan"):
                        if field in self.cache.get(device,{}): row[field]=self.cache[device][field]
                    old=self.cache.get(device,{})
                    for mode,assets in old.get("inactive_assets",{}).items():
                        before=old.get("profiles",{}).get(mode,{}).get("reference") or {}
                        after=row.get("profiles",{}).get(mode,{}).get("reference") or {}
                        if before.get("revision") is not None and before.get("revision")==after.get("revision"): row.setdefault("inactive_assets",{})[mode]=assets
                    if old.get("reference",{}).get("revision")==row.get("reference",{}).get("revision") and old.get("profile_context",{}).get("active_mode")==row.get("profile_context",{}).get("active_mode") and "thumbnails" in old: row["thumbnails"]=old["thumbnails"]
                    if old.get("reference",{}).get("revision")==row.get("reference",{}).get("revision") and old.get("profile_context",{}).get("active_mode")==row.get("profile_context",{}).get("active_mode") and "originals" in old: row["originals"]=old["originals"]
                    self.cache[device]=row
                self.jobs[jid]['status']='done'
            except Exception as exc:
                self.jobs[jid]["error_type"]=type(exc).__name__
                if kind=='refresh': self.cache[device]={**self.cache.get(device,{}),'online':False,'checked_at':time.time(),'error':'连接失败或超时；保留上次数据'}
                else: self.cache[device]={**self.cache.get(device,{}),('plan_error' if kind=='plan' else 'check_error'):'只读检查或安全门禁失败；请使用终端查看详细原因'}
                self.jobs[jid]['status']='failed'
            finally:
                try:
                    tmp=self.state/'cache.tmp'
                    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
                    with os.fdopen(fd,'w') as f: json.dump(self.cache,f,ensure_ascii=False,allow_nan=False)
                    os.replace(tmp,self.state/'cache.json')
                except OSError: self.jobs[jid]['status']='failed'
                finally:
                    try: self._save_jobs()
                    finally: self.guard.release()
        threading.Thread(target=run,daemon=True).start(); return jid
    def thumbnails(self,device,ids,original=False,mode=None):
        if device not in self.config['devices'] or not isinstance(ids,list) or not 1<=len(ids)<=(1 if original else 6) or any(not isinstance(x,str) for x in ids) or len(set(ids))!=len(ids): raise ValueError('invalid assets')
        row=self.cache.get(device,{})
        selection=row.get('profile_context') or {}; mode=mode or selection.get('active_mode')
        if mode not in ('emg','eeg'): raise ValueError('invalid mode')
        inactive=mode!=selection.get('active_mode')
        if inactive and not original: raise ValueError('inactive thumbnails unavailable')
        ref=(row.get('profiles',{}).get(mode,{}).get('reference') if inactive else row.get('reference')) or {}
        allowed={x.get('id') for x in ref.get('items',[]) if isinstance(x,dict)}
        if not row.get('online') or not all(isinstance(x,str) and x in allowed for x in ids): raise ValueError('refresh required')
        if not self.guard.acquire(False): raise RuntimeError('job busy')
        jid=uuid.uuid4().hex; self.jobs[jid]={'id':jid,'device':device,'status':'running','kind':'original' if original else 'thumbnails'}
        try: self._save_jobs()
        except OSError:
            self.guard.release(); raise RuntimeError('cannot persist job')
        def run():
            try:
                result=({ids[0]:metrics.fetch_original(self.config,device,mode,ref.get('revision'),ids[0],self.state)} if original else metrics.fetch_thumbnails(self.config,device,selection.get('active_mode'),ref.get('revision'),ids,self.state))
                (row.setdefault('inactive_assets',{}).setdefault(mode,{}) if inactive else row.setdefault('originals' if original else 'thumbnails',{})).update(result)
                self.jobs[jid]['status']='done'
                tmp=self.state/'cache.tmp'; fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
                with os.fdopen(fd,'w') as f: json.dump(self.cache,f,ensure_ascii=False,allow_nan=False)
                os.replace(tmp,self.state/'cache.json')
            except Exception: self.jobs[jid]['status']='failed'
            finally:
                try: self._save_jobs()
                finally: self.guard.release()
        threading.Thread(target=run,daemon=True).start(); return jid
    def apply(self,device,digest,typed,registry):
        operations.require_config(self.config,self.registry)
        import re
        if device not in self.config['devices'] or typed!=device or registry!=operations.REGISTRY or not re.fullmatch(r'[a-f0-9]{64}',digest or ''): raise ValueError('invalid request')
        plan=(self.cache.get(device,{}) or {}).get('review_plan') or {}
        if self.cache.get(device,{}).get('address')!=self.config['devices'][device] or plan.get('address')!=self.config['devices'][device] or plan.get('plan_sha256')!=digest or time.time()-plan.get('created_at',0)>600: raise ValueError('fresh reviewed plan required')
        auth=broker_client.status(self.state)
        if not auth.get('unlocked') or auth.get('registry_digest')!=self.registry.digest or auth.get('operation')!='apply_plan' or device not in auth.get('targets',[]): raise PermissionError('broker locked or registry changed')
        if not self.guard.acquire(False): raise RuntimeError('job busy')
        jid=uuid.uuid4().hex; self.jobs[jid]={'id':jid,'device':device,'kind':'apply','status':'queued','events':['queued'],'plan_sha256':digest}
        try: self._save_jobs()
        except OSError:
            self.guard.release(); raise RuntimeError('cannot persist')
        def event(stage):
            if stage not in ('preflight','applying','verifying'): return
            self.jobs[jid]['status']=stage; self.jobs[jid]['events'].append(stage); self._save_jobs()
        def run():
            try:
                result=broker_client.call(self.state,{'operation':'apply_plan','device':device,'plan_hash':digest,'registry_entry':registry},event,timeout=600)
                if 'result' in result:
                    summary=result['result']; status=summary.get('status')
                    if status not in ('succeeded','skipped_current'): raise ValueError('invalid result')
                    self.jobs[jid]['status']=status;self.jobs[jid]['receipt']=summary
                else:
                    code=result.get('error','broker_failed')
                    self.jobs[jid]['status']='partial_failed' if code=='partial_failed_reconcile_required' else 'failed'
                    self.jobs[jid]['error_code']=code if code in ('partial_failed_reconcile_required','preflight_or_auth_failed','locked_or_out_of_scope','configuration_changed') else 'broker_failed'
            except Exception:
                self.jobs[jid]['status']='interrupted_reconcile' if self.jobs[jid]['status'] in ('applying','verifying') else 'failed'
                self.jobs[jid]['error_code']='broker_disconnected'
            finally:
                self.jobs[jid]['events'].append(self.jobs[jid]['status'])
                try:self._save_jobs()
                finally:self.guard.release()
        threading.Thread(target=run,daemon=True).start();return jid
    def usb_read(self,device,kind,scan_id=None,ids=None):
        operations.require_config(self.config,self.registry)
        if device not in self.config['devices'] or kind not in ('usb_scan','usb_preview'):raise ValueError('invalid request')
        if not self.guard.acquire(False):raise RuntimeError('job busy')
        jid=uuid.uuid4().hex;self.jobs[jid]={'id':jid,'device':device,'kind':kind,'status':'running'}
        try:self._save_jobs()
        except OSError:self.guard.release();raise RuntimeError('cannot persist')
        def run():
            try:
                value=imports.scan(self.state,self.config,device) if kind=='usb_scan' else imports.preview(self.state,self.config,device,scan_id,ids)
                self.cache[device]={**self.cache.get(device,{}),('usb_scan' if kind=='usb_scan' else 'import_plan'):value}
                if kind=='usb_scan':self.cache[device].pop('import_plan',None)
                self.jobs[jid]['status']='done'
            except Exception as exc:
                code=str(exc);allowed={'scan_failed','invalid_scan','invalid_selection','scan_expired','unknown_zip_id','preview_failed','zip_size','unsafe_path','unsafe_member','expansion_limit','duplicate_path','duplicate_basename','member_size','pixel_limit','image_or_scene_count','usb_unmounted_or_identity_changed','insufficient_space','busy_privacy_or_context','catalog_changed','context_changed'}
                self.jobs[jid]['error_code']=code if code in allowed else 'remote_or_validation_failed';self.jobs[jid]['status']='failed'
            finally:
                try:
                    tmp=self.state/'cache.tmp';fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
                    with os.fdopen(fd,'w') as f:json.dump(self.cache,f,ensure_ascii=False,allow_nan=False)
                    os.replace(tmp,self.state/'cache.json');self._save_jobs()
                finally:self.guard.release()
        threading.Thread(target=run,daemon=True).start();return jid
    def import_apply(self,device,digest,typed,mode,keep):
        operations.require_config(self.config,self.registry)
        import re
        if device not in self.config['devices'] or typed!=device or keep is not True or mode not in ('emg','eeg') or not isinstance(digest,str) or not re.fullmatch(r'[a-f0-9]{64}',digest):raise ValueError('invalid request')
        row=self.cache.get(device,{}) or {};plan=row.get('import_plan') or {};context=row.get('profile_context') or {}
        if plan.get('plan_sha256')!=digest or plan.get('address')!=self.config['devices'][device] or plan.get('mode')!=mode or context.get('active_mode')!=mode or mode+':'+str(context.get('generation'))!=plan.get('context') or time.time()-plan.get('created_at',0)>600:raise ValueError('fresh active-profile plan required')
        auth=broker_client.status(self.state)
        if not auth.get('unlocked') or auth.get('registry_digest')!=self.registry.digest or auth.get('operation')!='import_tasks' or auth.get('plans',{}).get(device)!=digest:raise PermissionError('import broker locked or registry changed')
        if not self.guard.acquire(False):raise RuntimeError('job busy')
        jid=uuid.uuid4().hex;self.jobs[jid]={'id':jid,'device':device,'kind':'import','status':'queued','events':['queued'],'plan_sha256':digest}
        try:self._save_jobs()
        except OSError:self.guard.release();raise RuntimeError('cannot persist')
        def event(stage):
            import re
            if re.fullmatch(r'(building|ready|committing):\d{1,8}:\d{1,8}',stage or ''):
                phase,done,total=stage.split(':');self.jobs[jid]['phase']=phase;self.jobs[jid]['done']=int(done);self.jobs[jid]['total']=int(total)
                if not self.jobs[jid]['events'] or self.jobs[jid]['events'][-1]!=phase:self.jobs[jid]['events'].append(phase)
                self._save_jobs()
            elif stage in ('preflight','applying','verifying'):
                self.jobs[jid]['status']=stage;self.jobs[jid]['events'].append(stage);self._save_jobs()
        def run():
            try:
                out=broker_client.call(self.state,{'operation':'import_tasks','device':device,'plan_hash':digest,'registry_entry':imports.REGISTRY},event,timeout=1380)
                result=out.get('result') or {};status=result.get('status')
                if status in ('succeeded','failed','partial_failed','interrupted_reconcile'):
                    self.jobs[jid]['status']=status;self.jobs[jid]['receipt']={k:result.get(k) for k in ('status','reason','revision','verified_unique','old_lists_preserved','protected_unchanged') if k in result}
                else:self.jobs[jid]['status']='failed';self.jobs[jid]['error_code']=out.get('error','broker_failed')
            except Exception:
                self.jobs[jid]['status']='interrupted_reconcile' if self.jobs[jid]['status'] in ('applying','verifying') else 'failed';self.jobs[jid]['error_code']='broker_disconnected'
            finally:
                self.jobs[jid]['events'].append(self.jobs[jid]['status'])
                try:self._save_jobs()
                finally:self.guard.release()
        threading.Thread(target=run,daemon=True).start();return jid
