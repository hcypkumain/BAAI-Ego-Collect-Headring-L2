#!/usr/bin/env python3
"""Serve boot progress before importing the acquisition application."""
import json
from pathlib import Path
import runpy
import signal
import subprocess
import sys
import time

import startup_status

BASE=Path(__file__).resolve().parents[3]


def service_state(name):
    try:
        result=subprocess.run(['systemctl','show',name,'--property=ActiveState','--value'],
                              capture_output=True,text=True,timeout=2)
        return result.stdout.strip() if result.returncode == 0 else 'unknown'
    except (OSError,subprocess.TimeoutExpired):
        return 'unknown'


def clock_service_state():
    try:
        result=subprocess.run(['systemctl','show','headring-lan-time.service','--property=LoadState','--property=ActiveState'],capture_output=True,text=True,timeout=2)
        values=dict(line.split('=',1)for line in result.stdout.splitlines()if'='in line)
        if result.returncode or values.get('LoadState')=='not-found':return None
        return values.get('ActiveState','inactive')
    except (OSError,subprocess.TimeoutExpired):
        return 'unknown'


def wait_for_hardware(timeout=60, camera_socket=Path('/run/camd.sock'), state=service_state,
                      clock=time.monotonic, sleep=time.sleep):
    if state('egorecord.service') == 'active':
        raise RuntimeError('EgoRecord is running; cannot start another camera owner')
    deadline=clock()+timeout
    previous=None
    while True:
        if camera_socket.is_socket() and state('camd.service') == 'active':
            startup_status.phase('camera_ready','采集设备已就绪，正在加载采集界面。')
            return
        if clock() >= deadline:
            raise TimeoutError('camd did not become ready within the startup deadline')
        stage='waiting_clock' if state('headring-lan-time.service') in ('activating','reloading') else 'waiting_camera'
        if stage != previous:
            startup_status.phase(stage,'正在校准设备时间。' if stage=='waiting_clock' else '正在初始化头环相机。')
            previous=stage
        sleep(.2)


def wait_for_camera_start(timeout=60, state=service_state, clock=time.monotonic, sleep=time.sleep,
                          time_service=clock_service_state):
    """Allow imports/metadata preparation after clock alignment, alongside camd."""
    if state('egorecord.service') == 'active':
        raise RuntimeError('EgoRecord is running; cannot start another camera owner')
    deadline=clock()+timeout
    startup_status.phase('waiting_clock','正在准备设备时间和相机服务。')
    while time_service() not in (None,'active','failed') or state('camd.service') not in ('activating','active'):
        if clock() >= deadline:
            raise TimeoutError('Camera start job did not pass its dependency barrier')
        sleep(.2)


def run(base=BASE, settings=None, wait=wait_for_camera_start, launch=None, ready=wait_for_hardware):
    if settings is None:
        import yaml
        settings=(yaml.safe_load((base/'configs/devices.yaml').read_text(encoding='utf-8')) or {}).get('web_ui',{})
    visible=bool(settings.get('enabled',False))
    if visible:
        startup_status.begin(base,settings)
        startup_status.guard_handoff(ready)
    try:
        wait()
        if not visible:
            ready()
        startup_status.phase('loading_controller','正在加载采集界面和任务记录。')
        if launch is not None:
            return launch()
        runpy.run_path(str(Path(__file__).with_name('launcher.py')),run_name='__main__')
    except BaseException as error:
        if not isinstance(error,SystemExit) or error.code not in (None,0):
            startup_status.phase('failed','采集程序启动未完成，系统将自动重试。')
        raise
    finally:
        startup_status.close()


def main():
    def stop(*_args):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM,stop)
    signal.signal(signal.SIGINT,stop)
    run()


if __name__=='__main__':
    main()
