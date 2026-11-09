"""Registered manual hardware test; the collection service must be stopped."""
import sys,os,fcntl,json,subprocess
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'src/orangepi_collect/common'))
from project_paths import BASE,CODE_BASE,SRC,devices


def main():
    from scan_registry import require_member
    from device_discovery import fingerprint
    from scan_result import decode
    import uuid
    owner=require_member()
    fingerprint_wire=json.loads(json.dumps(fingerprint(devices().get('auto_discovery',{}))))
    request_id=uuid.uuid4().hex
    env=dict(os.environ,ORANGEPI_SCAN_REQUEST=json.dumps(dict(id=request_id,fingerprint=fingerprint_wire)))
    fd=os.open(devices()['emg']['emg_left']['port'],os.O_RDWR|os.O_NOCTTY|os.O_NONBLOCK)
    try:
        fcntl.ioctl(fd,0x540c)
        result=subprocess.run([str(BASE/'.venvs/hardware/bin/python'),str(SRC/'acquisition/scan_devices.py')],capture_output=True,text=True,check=True,timeout=15,env=env)
        rows=decode(result.stdout,owner['run'],owner['nonce'],request_id,fingerprint_wire)
        assert not next(row for row in rows if row['name']=='emg_left')['connected']
        print('PASS: occupied EMG is reported unavailable')
    finally:
        fcntl.ioctl(fd,0x540d);os.close(fd)


if __name__=='__main__':
    if sys.argv[1:]==['--registered-child']:
        import signal
        signal.signal(signal.SIGTERM,lambda *_:None)
        main()
    elif not sys.argv[1:]:
        # The controller owns this independent fixed lock throughout service.
        lock=(CODE_BASE/'runtime/launcher.lock').open('a+')
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('采集服务正在运行；请在受控维护窗口停止服务后重试')
        sys.path.insert(0,str(SRC/'control'))
        from scan_process import run_scan
        from scan_registry import Registry
        output=run_scan([str(BASE/'.venvs/hardware/bin/python'),str(Path(__file__).resolve()),'--registered-child'],
            Registry(CODE_BASE/'runtime'),kind='diagnostic',timeout=25)
        print(output,end='')
    else:
        raise SystemExit('无效诊断参数')
