"""Bounded diagnostic sampler; no system settings are modified."""
import argparse,json,time,os
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--seconds',type=int,default=300);a=p.parse_args()
start=time.monotonic()
with open(a.output,'w',buffering=1) as f:
 while time.monotonic()-start<a.seconds:
  row={'time':time.time(),'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'uptime':Path('/proc/uptime').read_text().strip(),'load':Path('/proc/loadavg').read_text().strip()}
  row['meminfo']={line.split(':')[0]:line.split(':')[1].strip() for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith(('MemAvailable:','SwapFree:'))}
  row['temperature']=Path('/sys/class/thermal/thermal_zone0/temp').read_text().strip()
  row['cpu_stat']=Path('/proc/stat').read_text().splitlines()[0]
  f.write(json.dumps(row)+'\n');f.flush();os.fsync(f.fileno())
  time.sleep(1)
