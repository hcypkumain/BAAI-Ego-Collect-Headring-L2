"""Persistent, fail-closed USB export transaction runner.

Handlers must be idempotent: power can fail after a handler returns but before
its next state is committed. The caller is responsible for holding the capture
and USB mount exclusions for the full lifetime of run().
"""
import copy
import fcntl
import os
import threading
import time
from pathlib import Path

from cloud_common import CloudError, atomic, read_json

STAGES = ('preparing', 'copying', 'verifying', 'committed',
          'ledger_committed', 'purging', 'complete')


class ExportJob:
    def __init__(self, base, handlers):
        self.root = Path(base) / 'runtime/usb_exports'
        self.path = self.root / 'pending.json'
        self.handlers = handlers
        self.lock = threading.RLock()
        self.thread = None
        self.root.mkdir(parents=True, exist_ok=True)

    def status(self):
        with self.lock:
            if self.path.is_symlink():
                raise CloudError('导出事务记录不安全')
            value = read_json(self.path)
            if value is None:
                return None
            if (not isinstance(value, dict) or value.get('version') != 1
                    or value.get('state') not in STAGES):
                raise CloudError('导出事务记录无效，禁止采集')
            return copy.deepcopy(value)

    def create(self, identity):
        with self.lock:
            old = self.status()
            if old is not None and old['state'] != 'complete':
                raise CloudError('已有未完成的导出事务')
            if not isinstance(identity, dict) or not identity:
                raise CloudError('缺少导出身份信息')
            value = dict(version=1, state='preparing', identity=copy.deepcopy(identity),
                         done_bytes=0, total_bytes=0, error='', updated_at=time.time())
            atomic(self.path, value)
            return copy.deepcopy(value)

    def progress(self, done, total):
        if type(done) is not int or type(total) is not int or not 0 <= done <= total:
            raise CloudError('导出进度无效')
        with self.lock:
            value = self.status()
            if value is None or value['state'] == 'complete':
                raise CloudError('导出事务不存在')
            value.update(done_bytes=done, total_bytes=total, updated_at=time.time())
            atomic(self.path, value)

    def run(self):
        # Serialize separate process instances as well as threads.
        fd = os.open(str(self.root/'worker.lock'), os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise CloudError('导出任务已经运行')
            while True:
                value = self.status()
                if value is None: raise CloudError('导出事务不存在')
                state = value['state']
                if state == 'complete': return value
                handler = self.handlers.get(state)
                if not callable(handler): raise CloudError('导出恢复处理器未就绪：' + state)
                try:
                    handler(copy.deepcopy(value), self.progress)
                except Exception as error:
                    with self.lock:
                        current = self.status()
                        current.update(error=str(error)[:500], updated_at=time.time())
                        atomic(self.path, current)
                    raise
                with self.lock:
                    current = self.status()
                    if current['state'] != state or current['identity'] != value['identity']:
                        raise CloudError('导出事务已变化，禁止继续')
                    current.update(state=STAGES[STAGES.index(state)+1], error='', updated_at=time.time())
                    atomic(self.path, current)
        finally:
            os.close(fd)

    def start(self):
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                raise CloudError('导出任务已经运行')
            def background():
                try: self.run()
                except Exception: pass  # persisted in the transaction when possible
            self.thread = threading.Thread(target=background, name='usb-export', daemon=True)
            self.thread.start()
