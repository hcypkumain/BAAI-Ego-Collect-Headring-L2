"""Own the privileged mount helper and its operation lock until unmount."""
import json
import os
import select
import subprocess
import time
from pathlib import Path
from cloud_common import CloudError
from usb_export_identity import UsbIdentity

HELPER='/usr/local/lib/collector-usb-task-import/mount-export'

class MountLease:
    def __init__(self, command=None, timeout=30):
        self.command=command or ['sudo','-n',HELPER];self.timeout=timeout
        self.process=None;self.usb=None;self.details=None

    def __enter__(self):
        self.process=subprocess.Popen(self.command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,bufsize=0)
        try:
            deadline=time.monotonic()+self.timeout;raw=bytearray()
            while b'\n' not in raw:
                wait=deadline-time.monotonic()
                if wait<=0:raise CloudError('U盘自动挂载超时')
                ready,_,_=select.select([self.process.stdout],[],[],wait)
                if not ready:raise CloudError('U盘自动挂载超时')
                block=os.read(self.process.stdout.fileno(),1)
                if not block:
                    raise CloudError('U盘自动挂载失败，请检查U盘和挂载权限')
                raw.extend(block)
                if len(raw)>8192:raise CloudError('U盘挂载结果无效')
            details=json.loads(raw)
            if (not isinstance(details,dict) or set(details)!={'device','mountpoint','uuid','label','major','minor','mount_id'}
                    or not isinstance(details['uuid'],str) or not details['uuid']
                    or type(details['major']) is not int or type(details['minor']) is not int or type(details['mount_id']) is not int):
                raise CloudError('U盘挂载身份无效')
            usb=UsbIdentity(details['mountpoint'],details['uuid'])
            if (usb.mount_id!=details['mount_id'] or
                (os.major(usb.device),os.minor(usb.device))!=(details['major'],details['minor'])):
                usb.close();raise CloudError('U盘挂载身份不一致')
            self.details,self.usb=details,usb
            return usb
        except BaseException:
            self._release(suppress=True);raise

    def _release(self,suppress=False):
        if self.usb is not None:self.usb.close();self.usb=None
        process=self.process;self.process=None
        if process is None:return
        try:
            if process.stdin:process.stdin.close()
            try:code=process.wait(timeout=40)
            except subprocess.TimeoutExpired as error:raise CloudError('U盘安全卸载超时，请勿拔出') from error
            if code:raise CloudError('U盘安全卸载未确认，请勿拔出')
        except (OSError,CloudError):
            if not suppress:raise
        finally:
            if process.stdout:process.stdout.close()
            if process.stderr:process.stderr.close()

    def __exit__(self, typ, value, tb):
        self._release(suppress=typ is not None)
