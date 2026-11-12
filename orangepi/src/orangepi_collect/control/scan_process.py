"""Bounded ownership and cleanup of one device-scan process group."""
import os
import signal
import subprocess
import tempfile
import time
from scan_registry import spawn_owned, RegistryError, proc_identity


class ScanProcessError(RuntimeError):
    def __init__(self, message, cleanup_pending=False, pgid=None, run=None, child=None, partial_output=None, ownership_blocked=False):
        super().__init__(message)
        self.cleanup_pending = cleanup_pending
        self.pgid = pgid
        self.run = run
        self.child = child
        self.partial_output = partial_output
        self.ownership_blocked = ownership_blocked


def group_alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def run_scan(command, registry, timeout=15, grace=.5, kill_grace=1.0, kind="scan", input_text=None, allow_nonzero=False, return_code=False, allow_partial=False, env=None, return_identity=False, cancel=None):
    # Regular files mean a surviving descendant cannot keep communicate() stuck
    # by retaining stdout/stderr pipes. The session belongs only to this scan.
    with tempfile.TemporaryFile(mode='w+t', encoding='utf-8') as out, tempfile.TemporaryFile(mode='w+t', encoding='utf-8') as err, tempfile.TemporaryFile(mode='w+t', encoding='utf-8') as source:
        if input_text is not None:source.write(input_text);source.seek(0)
        try:child,run = spawn_owned(registry,kind,command,stdin=source,stdout=out,stderr=err,env=env)
        except (RegistryError,OSError) as exc:
            # A failed reservation did not start a child. Do not turn a
            # historical registry barrier into this controller's permanent
            # pending-owner state (which has no run to retry).
            raise ScanProcessError(str(exc),False,ownership_blocked=True) from exc
        pgid = child.pid
        try:owner=registry.inspect()
        except (RegistryError,OSError) as exc:
            raise ScanProcessError('扫描所有权无法确认，保持设备锁定：'+str(exc),True,pgid,run,child,ownership_blocked=True) from exc
        if not owner or owner.get('run')!=run:raise ScanProcessError('扫描所有权已变化',True,pgid,run,child)
        nonce=owner.get('nonce')
        timed_out = False;cancelled=False;deadline=time.monotonic()+timeout
        while True:
            if cancel is not None:
                try:cancelled=bool(cancel())
                except Exception:cancelled=True
                if cancelled:break
            remaining=deadline-time.monotonic()
            if remaining<=0:
                timed_out=True;break
            try:
                child.wait(timeout=min(.1,remaining));break
            except subprocess.TimeoutExpired:pass
        # Clean up the entire dedicated group, including the v4l2-ctl children
        # of individual probes, even if the scan leader already exited.
        for sig, window in ((signal.SIGTERM, grace), (signal.SIGKILL, kill_grace)):
            if not group_alive(pgid):
                break
            # Never signal after reaping the leader: PID/PGID reuse is then
            # possible. Before reaping, its PID remains reserved by the kernel.
            if child.returncode is not None:break
            # Signal only while this controller owns the registered, still-live
            # group leader; never signal a historical numeric PGID.
            try:
                value=registry.verify_owner(run,kind);leader=proc_identity(pgid)
                if not leader or leader['start']!=value['wrapper_start'] or leader['state'] in ('Z','X'):
                    break
                os.killpg(pgid, sig)
            except (RegistryError,ProcessLookupError):break
            deadline = time.monotonic() + window
            while time.monotonic() < deadline:
                child.poll()  # reap the leader if it has already exited
                if not group_alive(pgid): break
                time.sleep(.02)
        child.poll()
        if group_alive(pgid) or child.returncode is None:
            out.seek(0);partial=out.read(262145)
            error=ScanProcessError('设备进程仍在清理，禁止重新扫描和开始采集', True, pgid, run, child, partial)
            error.nonce=nonce;raise error
        try:registry.finish(run)
        except (RegistryError,OSError) as exc:raise ScanProcessError(str(exc),True,pgid,run,child) from exc
        if cancelled:raise ScanProcessError('设备扫描已中止，等待优先控制操作')
        out.seek(0)
        stdout = out.read(262145)
        if len(stdout.encode('utf-8'))>262144:raise ScanProcessError('扫描结果超过长度限制')
        if timed_out and not (allow_partial and stdout.strip()):
            raise ScanProcessError('设备扫描超时')
        if child.returncode != 0 and not allow_nonzero and not (timed_out and allow_partial and stdout.strip()):
            raise ScanProcessError('设备扫描失败，请查看驱动服务')
        if return_identity:return stdout,run,nonce
        return (stdout,child.returncode) if return_code else stdout
