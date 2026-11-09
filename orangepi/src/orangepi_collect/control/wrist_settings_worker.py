"""Registered wrist-control transaction, run only while capture is released."""
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'common'))
from project_paths import BASE,devices
import wrist_camera_settings as settings
from web_controls import WebControls


def run(value):
    if value is not None and type(value) is not bool:raise ValueError('无效的腕部相机帧率选项')
    with settings.preference_lock(BASE),settings.capture_guard(BASE):
        reports=settings.configure_devices(devices(),value)
        WebControls().save_wrist_settings(value,reports)
        return reports


if __name__=='__main__':
    from scan_registry import require_member
    require_member()
    import signal
    signal.signal(signal.SIGTERM,lambda *_:None)
    try:
        result=run(json.load(sys.stdin))
        print(json.dumps(dict(ok=True,reports=result),ensure_ascii=False))
    except Exception as error:
        print(json.dumps(dict(ok=False,error=str(error),reports=getattr(error,'reports',None) or {}),ensure_ascii=False))
