"""Internal-disk receiver ledger. No filesystem deletion or cloud success guesses."""
import contextlib
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time

TARGETS=frozenset('cp_'+str(n).zfill(4) for n in (7,11,13,14,15,17,18,19,20,21,22,23))

class Conflict(ValueError):pass

class ReceiverStore:
    def __init__(self,root,clock=time.time):
        self.root=Path(root);self.clock=clock;self.lock=threading.RLock()
        if self.root.is_symlink():raise ValueError('Receiver ledger root must not be linked')
        self.root.mkdir(parents=True,exist_ok=True);self.path=self.root/'receiver.sqlite3'
        if self.path.is_symlink():raise ValueError('Receiver database must not be linked')
        self.key_path=self.root/'admin-key'
        if self.key_path.is_symlink():raise ValueError('Admin key must not be linked')
        if not self.key_path.exists():
            fd=os.open(str(self.key_path),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as f:f.write(secrets.token_urlsafe(32));f.flush();os.fsync(f.fileno())
        self.admin_key=self.key_path.read_text().strip()
        if len(self.admin_key)<32:raise ValueError('Invalid operator key')
        self.failures={}
        # SQLite cannot switch journal mode inside BEGIN IMMEDIATE.
        connection=sqlite3.connect(self.path,timeout=15)
        try:connection.execute('PRAGMA journal_mode=WAL')
        finally:connection.close()
        with self.db() as c:
            c.executescript('''
              CREATE TABLE IF NOT EXISTS control(id INTEGER PRIMARY KEY CHECK(id=1),receiving INTEGER NOT NULL,revision INTEGER NOT NULL,updated REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,response TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY,device TEXT NOT NULL,session TEXT NOT NULL,details TEXT NOT NULL,record TEXT NOT NULL,UNIQUE(device,session));
              CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY,record TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS auth_sessions(digest TEXT PRIMARY KEY,expires REAL NOT NULL);
            ''')
            c.execute('INSERT OR IGNORE INTO control VALUES(1,0,0,?)',(clock(),))
        self.recover()

    @contextlib.contextmanager
    def db(self):
        with self.lock:
            c=sqlite3.connect(self.path,timeout=15);c.row_factory=sqlite3.Row
            c.execute('PRAGMA synchronous=FULL');c.execute('PRAGMA busy_timeout=15000')
            try:
                c.execute('BEGIN IMMEDIATE');yield c;c.commit()
            except BaseException:c.rollback();raise
            finally:c.close()

    @staticmethod
    def _encode(value):return json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False)

    def control(self):
        with self.db() as c:
            r=c.execute('SELECT * FROM control WHERE id=1').fetchone()
            if r is None or r['receiving'] not in (0,1) or r['revision']<0:
                raise ValueError('Invalid persisted receiver control; refusing I/O')
            return dict(receiving=bool(r['receiving']),revision=r['revision'],updated=r['updated'])

    def set_control(self,receiving,revision,operation_id):
        if type(receiving) is not bool or type(revision) is not int or revision<0:
            raise ValueError('Invalid receiver intent')
        if not isinstance(operation_id,str) or not re.fullmatch(r'[0-9a-f-]{32,36}',operation_id):
            raise ValueError('Invalid operation identifier')
        with self.db() as c:
            old=c.execute('SELECT response FROM operations WHERE id=?',(operation_id,)).fetchone()
            if old:
                response=json.loads(old[0])
                if response['requested_receiving']!=receiving:raise Conflict('Operation id reused for another intent')
                current=c.execute('SELECT * FROM control WHERE id=1').fetchone()
                return dict(receiving=bool(current['receiving']),requested_receiving=receiving,
                    revision=current['revision'],updated=current['updated'],
                    operation_revision=response['revision'],idempotent=True)
            state=c.execute('SELECT * FROM control WHERE id=1').fetchone()
            if state['revision']!=revision:raise Conflict('Receiver control changed; refresh before retrying')
            next_revision=revision+int(bool(state['receiving'])!=receiving)
            c.execute('UPDATE control SET receiving=?,revision=?,updated=? WHERE id=1',
                (int(receiving),next_revision,self.clock()))
            response=dict(receiving=receiving,requested_receiving=receiving,revision=next_revision,updated=self.clock())
            c.execute('INSERT INTO operations VALUES(?,?)',(operation_id,self._encode(response)))
            return response

    def login(self,key,remote):
        now=self.clock()
        with self.lock:
            failures=[t for t in self.failures.get(remote,[]) if now-t<60]
            if len(failures)>=8:raise PermissionError('Too many login attempts; wait one minute')
            if not isinstance(key,str) or not hmac.compare_digest(key,self.admin_key):
                self.failures[remote]=failures+[now];raise PermissionError('Operator key rejected')
            self.failures.pop(remote,None)
        token=secrets.token_urlsafe(32);digest=hashlib.sha256(token.encode()).hexdigest()
        with self.db() as c:
            c.execute('DELETE FROM auth_sessions WHERE expires<?',(now,))
            c.execute('INSERT INTO auth_sessions VALUES(?,?)',(digest,now+24*3600))
        return token,self.csrf(token)

    def authorized(self,token):
        if not isinstance(token,str) or not 32<=len(token)<=100:return False
        with self.db() as c:
            row=c.execute('SELECT expires FROM auth_sessions WHERE digest=?',
                (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
            return row is not None and row[0]>self.clock()

    def csrf(self,token):
        return hmac.new(self.admin_key.encode(),('csrf:'+token).encode(),hashlib.sha256).hexdigest()

    def enqueue(self,device,details):
        sid=details.get('session_id','')
        if device not in TARGETS or not re.fullmatch(r'L[23]-[A-Za-z0-9_.-]{1,123}',sid):
            raise ValueError('Device/session outside explicit receive scope')
        with self.db() as c:
            old=c.execute('SELECT id,details FROM jobs WHERE device=? AND session=?',(device,sid)).fetchone()
            if old:
                if json.loads(old['details']).get('native_sha256')!=details.get('native_sha256'):
                    raise Conflict('Session name was reused for different native data')
                return old['id']
            record=dict(receive_state='copy_only_ready',release_state='hold_copy_only',process_state='hold_copy_only',
                reply_state='hold_copy_only',standard_state='hold_copy_only',cloud_state='not_configured',
                cloud_receipt_verified=False,cloud_confirmed_at=None,pc_delete_after=None,
                retention_hold=True,pc_retention_seconds=3*86400,created=self.clock(),updated=self.clock())
            cursor=c.execute('INSERT INTO jobs(device,session,details,record) VALUES(?,?,?,?)',
                (device,sid,self._encode(details),self._encode(record)))
            return cursor.lastrowid

    def _job(self,row):
        return dict(json.loads(row['record']),id=row['id'],device=row['device'],session=row['session'],
                    details=json.loads(row['details']))

    def jobs(self):
        values=[]
        with self.db() as c:
            for row in c.execute('SELECT * FROM jobs ORDER BY id DESC'):
                try:values.append(self._job(row))
                except (ValueError,TypeError):
                    values.append(dict(id=row['id'],device=row['device'],session=row['session'],details={},
                        receive_state='blocked',release_state='retained',process_state='blocked',
                        reply_state='blocked',cloud_state='not_configured',retention_hold=True,
                        receive_error='Persisted job record is invalid; originals retained'))
        return values

    def job(self,identifier):
        with self.db() as c:
            row=c.execute('SELECT * FROM jobs WHERE id=?',(identifier,)).fetchone()
            if row is None:raise KeyError(identifier)
            return self._job(row)

    def update(self,identifier,**values):
        if set(values)&{'id','device','session','details'}:raise ValueError('Cannot change job identity')
        with self.db() as c:
            row=c.execute('SELECT record FROM jobs WHERE id=?',(identifier,)).fetchone()
            if row is None:raise KeyError(identifier)
            record=json.loads(row[0]);record.update(values,updated=self.clock())
            c.execute('UPDATE jobs SET record=? WHERE id=?',(self._encode(record),identifier))

    def commit_copy(self,identifier,revision,raw_path,receipt,receipt_sha256):
        """Persist copy-only custody and STOP CAS in the same SQLite transaction."""
        with self.db() as c:
            state=c.execute('SELECT receiving,revision FROM control WHERE id=1').fetchone()
            if not state or not state['receiving'] or state['revision']!=revision:
                raise Conflict('STOP won before copy receipt commit')
            row=c.execute('SELECT * FROM jobs WHERE id=?',(identifier,)).fetchone()
            if row is None:raise KeyError(identifier)
            record=json.loads(row['record'])
            if record.get('raw_receipt') or record.get('release_decision_at'):
                raise ValueError('Existing handoff transaction requires review')
            encoded=json.dumps(receipt,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
            if hashlib.sha256(encoded).hexdigest()!=receipt_sha256:raise ValueError('Copy receipt hash mismatch')
            if (receipt.get('purpose')!='host_raw_copy_only_v1' or receipt.get('device_id')!=row['device']
                    or receipt.get('session_id')!=row['session'] or receipt.get('host_path')!=raw_path):
                raise ValueError('Copy receipt does not match the job')
            prior=record.get('copy_receipt')
            if prior is not None and (prior!=receipt or record.get('copy_receipt_sha256')!=receipt_sha256):
                raise ValueError('Different copy receipt already recorded')
            record.update(raw_path=raw_path,copy_receipt=receipt,copy_receipt_sha256=receipt_sha256,
                receive_state='host_committed',release_state='hold_copy_only',process_state='hold_copy_only',
                reply_state='hold_copy_only',standard_state='hold_copy_only',retention_hold=True,receive_error=None,updated=self.clock())
            c.execute('UPDATE jobs SET record=? WHERE id=?',(self._encode(record),identifier))

    def authorize_release(self,identifier,revision):
        raise ValueError('Copy-only mode never authorizes source release')

    def device(self,identifier,**values):
        if identifier not in TARGETS:raise ValueError('Device outside receive scope')
        values.update(id=identifier,updated=self.clock())
        with self.db() as c:c.execute('INSERT OR REPLACE INTO devices VALUES(?,?)',
            (identifier,self._encode(values)))

    def devices(self):
        with self.db() as c:return [json.loads(r[0]) for r in c.execute('SELECT record FROM devices ORDER BY id')]

    def claim(self,stage):
        fields={'receive':'receive_state','release':'release_state','process':'process_state','reply':'reply_state'}
        if stage not in fields:raise ValueError('Unknown receiver stage')
        if stage!='receive':return None
        field=fields[stage];now=self.clock()
        with self.db() as c:
            receiving=bool(c.execute('SELECT receiving FROM control WHERE id=1').fetchone()[0])
            if stage in ('receive','reply') and not receiving:return None
            rows=c.execute('SELECT * FROM jobs ORDER BY id').fetchall()
            busy_devices=set()
            if stage=='receive':
                for active in rows:
                    try:
                        if json.loads(active['record']).get('receive_state')=='copy_only_running':busy_devices.add(active['device'])
                    except (ValueError,TypeError):pass
            for row in rows:
                try:job=self._job(row);state=job[field]
                except (ValueError,TypeError,KeyError):continue
                eligible=(stage=='receive' and state in ('copy_only_ready','copy_only_retry','copy_only_paused')
                    or stage=='release' and state in ('retained','retry_wait') and bool(job.get('raw_receipt'))
                    or stage=='process' and state in ('queued','retry_wait') and bool(job.get('raw_receipt'))
                    or stage=='reply' and state in ('queued','retry_wait') and job['release_state']=='deleted' and bool(job.get('result')))
                if not eligible or job.get(stage+'_available',0)>now:continue
                if stage=='receive' and job['device'] in busy_devices:continue
                if stage=='release' and not receiving and not job.get('release_decision_at'):continue
                record=json.loads(row['record']);record[field]='copy_only_running';record[stage+'_attempts']=record.get(stage+'_attempts',0)+1
                record['updated']=now;c.execute('UPDATE jobs SET record=? WHERE id=?',(self._encode(record),row['id']))
                return dict(record,id=row['id'],device=row['device'],session=row['session'],details=job['details'])
            return None

    def recover(self):
        with self.db() as c:
            for row in c.execute('SELECT id,record FROM jobs').fetchall():
                try:record=json.loads(row['record'])
                except (ValueError,TypeError):continue
                if not isinstance(record,dict):continue
                changed=False
                if record.get('receive_state')=='copy_only_running':record['receive_state']='copy_only_retry';changed=True
                # Legacy running states are not converted into copy-only jobs.
                if changed:c.execute('UPDATE jobs SET record=? WHERE id=?',(self._encode(record),row['id']))
