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

from host.common.device_registry import Registry,receive_ids
from host.common.strict_json import strict_json_loads

class Conflict(ValueError):pass

class ReceiverStore:
    def __init__(self,root,clock=time.time,*,registry:Registry):
        self.registry=registry;self.receive_ids=receive_ids(registry)
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
                response=strict_json_loads(old[0])
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
        if device not in self.receive_ids or not re.fullmatch(r'L[23]-[A-Za-z0-9_.-]{1,123}',sid):
            raise ValueError('Device/session outside explicit receive scope')
        with self.db() as c:
            old=c.execute('SELECT id,details FROM jobs WHERE device=? AND session=?',(device,sid)).fetchone()
            if old:
                if strict_json_loads(old['details']).get('native_sha256')!=details.get('native_sha256'):
                    raise Conflict('Session name was reused for different native data')
                return old['id']
            record=dict(receive_state='discovered',release_state='retained',process_state='waiting_raw',
                reply_state='waiting_qc',standard_state='waiting_qc',cloud_state='not_configured',
                cloud_receipt_verified=False,cloud_confirmed_at=None,pc_delete_after=None,
                retention_hold=True,pc_retention_seconds=3*86400,created=self.clock(),updated=self.clock())
            cursor=c.execute('INSERT INTO jobs(device,session,details,record) VALUES(?,?,?,?)',
                (device,sid,self._encode(details),self._encode(record)))
            return cursor.lastrowid

    def _job(self,row):
        return dict(strict_json_loads(row['record']),id=row['id'],device=row['device'],session=row['session'],
                    details=strict_json_loads(row['details']))

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
            record=strict_json_loads(row[0]);record.update(values,updated=self.clock())
            c.execute('UPDATE jobs SET record=? WHERE id=?',(self._encode(record),identifier))

    def authorize_release(self,identifier,revision):
        """CAS the deletion decision with STOP in the SAME durable transaction."""
        with self.db() as c:
            state=c.execute('SELECT receiving,revision FROM control WHERE id=1').fetchone()
            if not state['receiving'] or state['revision']!=revision:
                raise Conflict('STOP won before raw handoff authorization')
            row=c.execute('SELECT record FROM jobs WHERE id=?',(identifier,)).fetchone()
            record=strict_json_loads(row[0])
            if not record.get('raw_receipt') or not record.get('raw_receipt_sha256'):
                raise ValueError('No committed raw custody receipt')
            record.update(release_decision_at=self.clock(),release_control_revision=revision)
            c.execute('UPDATE jobs SET record=? WHERE id=?',(self._encode(record),identifier))

    def device(self,identifier,**values):
        if identifier not in self.receive_ids:raise ValueError('Device outside receive scope')
        values.update(id=identifier,updated=self.clock())
        with self.db() as c:c.execute('INSERT OR REPLACE INTO devices VALUES(?,?)',
            (identifier,self._encode(values)))

    def devices(self):
        with self.db() as c:return [strict_json_loads(r[0]) for r in c.execute('SELECT record FROM devices ORDER BY id')]

    def claim(self,stage):
        fields={'receive':'receive_state','release':'release_state','process':'process_state','reply':'reply_state'}
        if stage not in fields:raise ValueError('Unknown receiver stage')
        field=fields[stage];now=self.clock()
        with self.db() as c:
            receiving=bool(c.execute('SELECT receiving FROM control WHERE id=1').fetchone()[0])
            if stage in ('receive','reply') and not receiving:return None
            rows=c.execute('SELECT * FROM jobs ORDER BY id').fetchall()
            # Same-device I/O is serialized in this durable claim transaction,
            # including callers in another process. A paused device never blocks
            # independent devices. Malformed persisted rows remain unclaimable.
            busy_devices=set()
            if stage in ('receive','release','reply'):
                for row in rows:
                    try:record=strict_json_loads(row['record'])
                    except (ValueError,TypeError):continue
                    if isinstance(record,dict) and any(record.get(f)=='running' for f in
                            ('receive_state','release_state','reply_state')):
                        busy_devices.add(row['device'])
            for row in rows:
                if row['device'] not in self.receive_ids or row['device'] in busy_devices:continue
                try:job=self._job(row);state=job[field]
                except (ValueError,TypeError,KeyError):continue
                eligible=(stage=='receive' and state in ('discovered','retry_wait','paused')
                    or stage=='release' and state in ('retained','retry_wait') and bool(job.get('raw_receipt'))
                    or stage=='process' and state in ('queued','retry_wait') and bool(job.get('raw_receipt'))
                    or stage=='reply' and state in ('queued','retry_wait') and job['release_state']=='deleted' and bool(job.get('result')))
                if not eligible or job.get(stage+'_available',0)>now:continue
                if stage=='release' and not receiving and not job.get('release_decision_at'):continue
                record=strict_json_loads(row['record']);record[field]='running';record[stage+'_attempts']=record.get(stage+'_attempts',0)+1
                record['updated']=now;c.execute('UPDATE jobs SET record=? WHERE id=?',(self._encode(record),row['id']))
                return dict(record,id=row['id'],device=row['device'],session=row['session'],details=job['details'])
            return None

    def recover(self):
        with self.db() as c:
            for row in c.execute('SELECT id,record FROM jobs').fetchall():
                try:record=strict_json_loads(row['record'])
                except (ValueError,TypeError):continue
                if not isinstance(record,dict):continue
                changed=False
                for field in ['receive_state','release_state','process_state','reply_state']:
                    if record.get(field)=='running':record[field]='retry_wait';changed=True
                if changed:c.execute('UPDATE jobs SET record=? WHERE id=?',(self._encode(record),row['id']))
