"""Drive the supplied uploader CLI with scoped inputs and persistent per-package proofs.

No cloud deletion, no PC cleanup, no fake completion. Old EGOL2 content is retained.
"""
import argparse
import builtins
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import sys
import time
import threading

from cloud_client import ScopedUploader,digest_file,SERVER
from cloud_scope import dataset_path,expected_standard_files

def atomic(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);temp=path.with_name(path.name+'.tmp')
    with temp.open('w') as f:json.dump(value,f,ensure_ascii=False,allow_nan=False);f.flush();os.fsync(f.fileno())
    temp.replace(path)
    fd=os.open(path.parent,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)

def files_for_package(root,entry):
    info=json.loads((root/'meta/info.json').read_text())
    if root.is_symlink() or root.resolve()!=root:raise ValueError('Linked standard package')
    if info.get('codebase_version')!='v3.1' or info.get('pi_export',{}).get('profile')!=entry['base_profile']:
        raise ValueError('Standard package identity/profile mismatch')
    expected_count={'L2':28,'L3':30}[entry['base_profile']]
    if entry['base_profile']=='L3':
        eeg=info.get('features',{}).get('observation.eeg',{})
        if eeg.get('shape')!=[8] or len(eeg.get('names') or [])!=8 or len(set(eeg['names']))!=8:
            raise ValueError('Only genuine eight-channel EEG may enter EGOL3_hand')
    files=[]
    for p in sorted(root.rglob('*')):
        if p.is_symlink():raise ValueError('Linked standard package member')
        if p.is_file():files.append(dict(path=p.relative_to(root).as_posix(),bytes=p.stat().st_size,sha256=digest_file(p)))
    if (len(files)!=expected_count or len(files)!=entry['files']
            or {row['path'] for row in files}!=expected_standard_files(entry['base_profile'])):
        raise ValueError('Fixed package file list/count mismatch')
    return files

class UploadQueue:
    def __init__(self,app_root,state_root,proxy):
        self.app=Path(app_root);self.state=Path(state_root);self.state.mkdir(parents=True,exist_ok=True)
        self.proxy=proxy;self.config=json.loads((self.app/'config.json').read_text());self.data=Path(self.config['data_root'])
        self.stop=threading.Event()
        self.std=self.data/'standardized';self.credentials=json.loads((self.app/'private/cloud-uploader.json').read_text())
        self.db=sqlite3.connect(self.state/'cloud-upload.sqlite3');self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS uploads(target TEXT PRIMARY KEY,source TEXT NOT NULL,profile TEXT NOT NULL,manifest TEXT NOT NULL,state TEXT NOT NULL,task_id INTEGER,receipt TEXT,error TEXT)');self.db.commit()

    def seed(self):
        catalog=json.loads((self.std/'_catalog.json').read_text())
        if catalog['root']!=str(self.std) or catalog.get('errors'):raise ValueError('Unverified standard package catalog')
        for entry in catalog['entries']:
            relative=entry['relative_path'];p=Path(relative)
            if p.is_absolute() or '..' in p.parts:raise ValueError('Unsafe catalog path')
            root=self.std/p;target=dataset_path(('EGOL2' if entry['base_profile']=='L2' else 'EGOL3_hand')+'/'+relative)
            old=self.db.execute('SELECT manifest FROM uploads WHERE target=?',(target,)).fetchone()
            if old:continue
            manifest=files_for_package(root,entry)
            self.db.execute('INSERT INTO uploads VALUES(?,?,?,?,?,?,?,?)',
                (target,str(root),entry['base_profile'],json.dumps(manifest), 'queued',None,None,None));self.db.commit()
        prepared=self.app/'integrations/robot-uploader-1.0.0/adapter/prepared-first-upload.json'
        if prepared.exists():
            row=json.loads(prepared.read_text())
            if row.get('upload_task_prepared'):
                saved=self.db.execute('SELECT manifest FROM uploads WHERE target=?',(row['target'],)).fetchone()
                if saved and json.loads(saved[0])==row['manifest']:
                    self.db.execute('UPDATE uploads SET task_id=? WHERE target=? AND task_id IS NULL',
                        (row['upload_task_id'],row['target']));self.db.commit()
        self.status()

    def status(self):
        counts=dict(self.db.execute('SELECT state,count(*) FROM uploads GROUP BY state'))
        value=dict(now=time.time(),counts=counts,total=sum(counts.values()),cloud_roots=['EGOL2','EGOL3_hand'],
            old_cloud_data_retained=True,pc_cleanup_performed=False,retention_seconds=259200)
        atomic(self.state/'status.json',value);return value

    def _safe_batch(self,sdk,source,target,manifest,task_id):
        binding=json.loads((self.app/'private/project-scope.json').read_text()).get(target.split('/')[0],{})
        if sdk.project_id!=binding.get('project_id') or binding.get('project_name')!=target.split('/')[0]:
            raise ValueError('CLI selected an unapproved project')
        client=ScopedUploader(self.app/'private/cloud-uploader.json',self.state/'sdk-state',
            proxy=self.proxy,authenticated_sdk=sdk,retry_unverified=True)
        if task_id is None:
            task_id=client.begin(source,target,manifest)
            self.db.execute('UPDATE uploads SET task_id=? WHERE target=?',(task_id,target));self.db.commit()
        for row in manifest:
            if self.stop.is_set():raise RuntimeError('Cloud worker stopping; partial transfer and PC data retained')
            client.upload_file(source,target,row)
        receipt=client.confirm(task_id,target,manifest)
        from pc_retention import Retention
        receipt=Retention(self.app/'state/pc-retention').seal(receipt,source,manifest,self.config['storage_uuid'])
        self.db.execute('UPDATE uploads SET state=?,receipt=?,error=NULL WHERE target=?',
            ('remote_confirmed',json.dumps(receipt),target));self.db.commit()
        atomic(self.state/'receipts'/(hashlib.sha256(target.encode()).hexdigest()+'.json'),receipt)

    def run_one(self,row):
        from robot_data_uploader import uploader as cli,config
        target,source,profile,manifest_raw,state,task_id,previous_receipt,error=row
        dataset_path(target);Path(source).resolve().relative_to(self.std.resolve())
        manifest=json.loads(manifest_raw);root_name=target.split('/')[0]
        config.SERVER_URL=SERVER;config.ENDPOINT='ks3-cn-beijing.ksyuncs.com'
        captured=io.StringIO();menus=0;completed=False;prompt_counts={}
        original_input=builtins.input;original_projects=cli.RobotDataUploader.get_project_list
        original_batch=cli.RobotDataUploader.batch_upload;original_detect=cli.detect_data_format
        def input_value(prompt=''):
            nonlocal menus
            plain=re.sub(r'\x1b\[[0-9;]*m','',str(prompt));print(plain,end='')
            prompt_counts[plain]=prompt_counts.get(plain,0)+1
            if plain.strip()!='>' and prompt_counts[plain]>1:
                raise RuntimeError('CLI repeated a failed input prompt; retaining data instead of looping')
            if plain.strip()=='>':menus+=1;return '2' if menus==1 else '7'
            if 'Access Key' in plain:return self.credentials['access_key']
            if 'Secret Key' in plain:return self.credentials['secret_key']
            if '关联的任务ID' in plain:return ''
            if '待上传目录路径' in plain:return source
            if '目标数据集路径' in plain:return target
            raise ValueError('Unexpected CLI prompt; no arbitrary project/path input supplied')
        def project_list(sdk):
            values=original_projects(sdk)
            if values is None:return None
            allowed=[p for p in values if p.get('projectName')==root_name]
            if len(allowed)!=1:raise ValueError('CLI project match is not exactly the authorized root')
            sdk.projects=allowed;return allowed
        def detect(directory):
            if str(Path(directory).resolve())!=str(Path(source).resolve()):raise ValueError('CLI source path changed')
            return 'LerobotDataset3.1'
        def safe_batch(sdk,directory,destination):
            nonlocal completed
            if directory!=source or destination!=target:raise ValueError('CLI changed authorized upload target')
            self._safe_batch(sdk,source,target,manifest,task_id);completed=True
        builtins.input=input_value;cli.RobotDataUploader.get_project_list=project_list
        cli.RobotDataUploader.batch_upload=safe_batch;cli.detect_data_format=detect
        self.db.execute('UPDATE uploads SET state=?,error=NULL WHERE target=?',('uploading',target));self.db.commit()
        try:
            with contextlib.redirect_stdout(captured),contextlib.redirect_stderr(captured):
                sys.argv=['robot-uploader'];cli.main()
            if not completed:raise RuntimeError('CLI returned without verified cloud confirmation')
        except (Exception,SystemExit) as exc:
            message=str(exc)
            for secret in self.credentials.values():
                if isinstance(secret,str) and secret:message=message.replace(secret,'[redacted]')
            self.db.execute('UPDATE uploads SET state=?,error=? WHERE target=?',('retry_wait',message[:2000],target));self.db.commit()
            raise
        finally:
            builtins.input=original_input;cli.RobotDataUploader.get_project_list=original_projects
            cli.RobotDataUploader.batch_upload=original_batch;cli.detect_data_format=original_detect
            text=re.sub(r'\x1b\[[0-9;]*m','',captured.getvalue())
            for secret in [self.credentials['access_key'],self.credentials['secret_key']]:text=text.replace(secret,'[redacted]')
            text=re.sub(r'eyJ[A-Za-z0-9_.-]{30,}','[token-redacted]',text)
            (self.state/(hashlib.sha256(target.encode()).hexdigest()+'.cli.log')).write_text(text,encoding='utf-8')
            self.status()

    def run(self,one=False):
        rows=self.db.execute('SELECT * FROM uploads WHERE state<>? ORDER BY task_id IS NULL,target',('remote_confirmed',)).fetchall()
        errors=[];consecutive_failures=0
        for row in rows:
            try:self.run_one(row);consecutive_failures=0
            except Exception as error:
                errors.append(dict(target=row[0],error_kind=type(error).__name__))
                consecutive_failures+=1
                if one or consecutive_failures>=3:break
            print('CLOUD_PROGRESS '+json.dumps(self.status()),flush=True)
            if one:break
        return dict(self.status(),errors=errors,all_confirmed=not errors and self.status()['counts'].get('remote_confirmed',0)==self.status()['total'])

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--app',type=Path,required=True)
    parser.add_argument('--state',type=Path,required=True);parser.add_argument('--proxy');parser.add_argument('--one',action='store_true')
    parser.add_argument('--scoped-dns',action='store_true');parser.add_argument('--watch',action='store_true')
    args=parser.parse_args()
    if args.scoped_dns:
        from scoped_dns import install
        install()
    args.state.mkdir(parents=True,exist_ok=True);os.chdir(args.state)
    import fcntl
    lock=(args.state/'worker.lock').open('a')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        print('CLOUD_RESULT '+json.dumps({'already_running':True,'new_upload_started':False}),flush=True);return
    queue=UploadQueue(args.app,args.state,args.proxy)
    signal.signal(signal.SIGTERM,lambda *a:queue.stop.set())
    signal.signal(signal.SIGINT,lambda *a:queue.stop.set())
    while not queue.stop.is_set():
        try:
            queue.seed();result=queue.run(args.one);print('CLOUD_RESULT '+json.dumps(result),flush=True)
        except Exception as error:
            print('CLOUD_WORKER_HOLD '+type(error).__name__,flush=True)
            if not args.watch:raise
        if not args.watch:break
        queue.stop.wait(60)
    lock.close()

if __name__=='__main__':main()
