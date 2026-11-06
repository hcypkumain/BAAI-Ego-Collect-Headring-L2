"""Manual hardware test; stop collection service first. Never records data."""
import sys,os,fcntl,json,subprocess
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'src/orangepi_collect/common'))
from project_paths import BASE,SRC,devices
if __name__=='__main__':
    fd=os.open(devices()['emg']['emg_left']['port'],os.O_RDWR|os.O_NOCTTY|os.O_NONBLOCK)
    try:
        fcntl.ioctl(fd,0x540c)
        result=subprocess.run([str(BASE/'.venvs/hardware/bin/python'),str(SRC/'acquisition/scan_devices.py')],capture_output=True,text=True,check=True,timeout=15)
        rows=json.loads(result.stdout)
        assert not next(r for r in rows if r['name']=='emg_left')['connected']
        print('PASS: occupied EMG is reported unavailable')
    finally:
        fcntl.ioctl(fd,0x540d);os.close(fd)
