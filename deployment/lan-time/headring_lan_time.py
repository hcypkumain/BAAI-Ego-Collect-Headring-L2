#!/usr/bin/env python3
"""Best-effort LAN clock sync. Acquisition always wins; no hardware data is edited."""
import argparse
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import socket
import stat
import struct
import subprocess
import time
import urllib.error
import urllib.request

BASE=Path('/home/orangepi/Desktop/Embodied-Brain-Collect-main.2/orangepi')
CONFIG=Path('/etc/headring-lan-time.json')
STATE=Path('/run/headring-lan-time.json')
ACQUISITION_LOCK=Path('/run/lock/headring-acquisition-time.lock')

@contextmanager
def exclusive_acquisition_lock():
 # Provisioned by root at boot. Never create, replace, or follow a symlink.
 fd=os.open(ACQUISITION_LOCK,os.O_RDONLY|os.O_CLOEXEC|os.O_NOFOLLOW)
 try:
  info=os.fstat(fd)
  if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
   raise OSError('invalid_acquisition_lock')
  if ACQUISITION_LOCK==Path('/run/lock/headring-acquisition-time.lock') and (info.st_uid!=0 or info.st_mode&0o022):
   raise OSError('insecure_acquisition_lock')
  fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
  current=os.stat(ACQUISITION_LOCK,follow_symlinks=False)
  if (current.st_dev,current.st_ino)!=(info.st_dev,info.st_ino):raise OSError('replaced_acquisition_lock')
  yield
 finally:os.close(fd)
SAFE={'ready','device_confirm','idle','failed_wait','exhausted'}

def verdict(display,started=False):
 if display.get('state') not in SAFE:return False,'workflow_busy'
 if (display.get('privacy') or {}).get('required'):return False,'privacy_or_recording_active'
 worker=display.get('worker') or {}
 if worker.get('recording_active'):return False,'recording_active'
 if worker.get('state') in {'recording','postprocessing','checking','saving','finalizing'}:return False,'saving_or_processing'
 if display.get('state')=='ready' and (started or worker.get('recording_active') is not False or worker.get('state')!='ready'):
  return False,'prepared_recording_has_started_or_is_uncertain'
 return True,'safe_idle'

def safe_to_step(boot=False):
 try:
  token=(BASE/'runtime/dashboard_token').read_text().strip()
  request=urllib.request.Request('http://127.0.0.1:8765/api/display-state',headers={'X-Access-Token':token})
  with urllib.request.urlopen(request,timeout=.7) as response:v=json.load(response)
  if not isinstance(v,dict):return False,'invalid_display_state'
  session=v.get('session')
  if session:
   if not isinstance(session,str):return False,'invalid_session'
   status=json.loads((Path(session)/'status.json').read_text())
   if not isinstance(status,dict) or not isinstance(status.get('eeg_impedance'),dict):return False,'invalid_impedance_status'
   impedance=status['eeg_impedance']
   if any(type(impedance.get(k)) is not bool for k in ('active','pending')):return False,'invalid_impedance_status'
   if impedance['active'] or impedance['pending']:return False,'eeg_impedance_measurement_active'
  started=bool(session and (Path(session)/'start_gate.json').exists())
  allowed,reason=verdict(v,started)
  if not allowed:return False,reason
  # A responding application may be an older process without the new lock.
  # Even idle UI state is not sufficient to permit a step.
  return False,'application_ui_active'
 except FileNotFoundError:
  # Only boot may proceed without a UI token, while holding the exclusive
  # acquisition lock; a missing session status must instead fail closed.
  if boot and not (BASE/'runtime/dashboard_token').exists():return True,'before_application_start'
  return False,'authoritative_status_unavailable'
 except urllib.error.HTTPError as error:
  if boot and error.code==503 and error.headers.get('X-HeadRing-Startup')=='1':return True,'startup_web_before_controller'
  return False,'authoritative_status_unavailable'
 except urllib.error.URLError as error:
  if boot and isinstance(error.reason,ConnectionRefusedError):return True,'before_application_start'
  return False,'authoritative_status_unavailable'
 except Exception:
  return False,'authoritative_status_unavailable'

def parse_reply(data,nonce,t0,t1):
 if len(data)<48 or data[0]&7!=4 or data[0]>>6==3 or not 1<=data[1]<=15:
  raise ValueError('server_not_synchronized_or_invalid_reply')
 if data[24:32]!=nonce:raise ValueError('reply_does_not_match_request')
 def epoch(part):
  sec,frac=struct.unpack('!II',part);return sec-2208988800+frac/2**32
 receive,transmit=epoch(data[32:40]),epoch(data[40:48])
 delay=(t1-t0)-(transmit-receive)
 if not math.isfinite(transmit) or delay<-.01 or delay>.5:raise ValueError('invalid_or_slow_measurement')
 return {'offset_seconds':((receive-t0)+(transmit-t1))/2,'rtt_seconds':max(0.,delay),'server_stratum':data[1]}

def measure(server,attempts=3):
 samples=[]
 for _ in range(attempts):
  s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.settimeout(1.)
  try:
   nonce=os.urandom(8);packet=bytearray(48);packet[0]=0x23;packet[40:48]=nonce
   s.connect((server,123));t0=time.time();s.send(packet);data=s.recv(512);t1=time.time()
   samples.append(parse_reply(data,nonce,t0,t1))
  except (OSError,ValueError):pass
  finally:s.close()
 if not samples:raise RuntimeError('LAN time server unavailable; continue collecting with current clock')
 return min(samples,key=lambda x:x['rtt_seconds'])

def write_state(value):
 tmp=STATE.with_suffix('.tmp');tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');os.replace(tmp,STATE)

def run(boot=False,dry_run=False):
 config=json.loads(CONFIG.read_text());server=config['server'];result={'server':server,'checked_at':time.time(),'policy':'continue_capture_if_unavailable'}
 deadline=time.monotonic()+config.get('boot_retry_seconds',8.)
 while True:
  try:
   sample=measure(server,1) if boot else measure(server)
   break
  except RuntimeError as error:
   if not boot or time.monotonic()>=deadline:
    result.update(status='unavailable',reason=str(error));return result
   # network-online.target may precede DHCP on these images. Briefly retry,
   # but never keep collection waiting indefinitely when the host is absent.
   time.sleep(.25)
 result.update(sample)
 try:
  with exclusive_acquisition_lock():
   allowed,reason=safe_to_step(boot=boot)
   if not allowed:result.update(status='deferred',reason=reason);return result
   needs_step=abs(sample['offset_seconds'])>config.get('step_threshold_seconds',.05)
   if dry_run:result.update(status='dry_run',would_step=needs_step);return result
   if needs_step:time.clock_settime(time.CLOCK_REALTIME,time.time()+sample['offset_seconds'])
   result.update(status='aligned' if needs_step else 'already_aligned',stepped=needs_step,after_epoch=time.time())
 except OSError as error:
  result.update(status='deferred',reason='acquisition_lock_unavailable_or_clock_error',detail=str(error));return result
 # These commands persist the clock, but do not alter CLOCK_REALTIME. Run them
 # outside the exclusion lock so hardware startup never waits for their timeout.
 if needs_step or boot:
  result['persistence']={}
  for name,args in [('rtc',['hwclock','--systohc','--utc']),('saved_clock',['fake-hwclock','save'])]:
   try:
    p=subprocess.run(args,capture_output=True,text=True,timeout=4);result['persistence'][name]={'rc':p.returncode,'error':p.stderr.strip()}
   except Exception as error:result['persistence'][name]={'error':str(error)}
 return result

def main():
 parser=argparse.ArgumentParser();parser.add_argument('--boot',action='store_true');parser.add_argument('--dry-run',action='store_true');args=parser.parse_args()
 lock=Path('/run/headring-lan-time.lock').open('a')
 try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 except BlockingIOError:return
 try:result=run(args.boot,args.dry_run)
 except Exception as error:result={'status':'unavailable','reason':str(error),'policy':'continue_capture_if_unavailable','checked_at':time.time()}
 try:write_state(result)
 except OSError:pass
 print(json.dumps(result,ensure_ascii=False),flush=True)
 # Missing server or unavailable clock persistence must not block collection.
 return 0

if __name__=='__main__':raise SystemExit(main())
