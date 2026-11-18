#!/usr/bin/python3
"""Recover DHCP on the uniquely selected phone interface without USB resets."""
import importlib.util
import ipaddress
import json
import re
from pathlib import Path
import subprocess
import time

BASE=str(Path(__file__).resolve().parent)
META=BASE+'/metadata.json'
LEGACY='10.42.0.2'

class RetryPolicy:
 def __init__(self):self.key=None;self.since=None;self.next_try=0;self.attempts=0;self.carrier=False
 def observe(self,key,addresses,carrier,method,now):
  if key!=self.key:self.key=key;self.since=now;self.next_try=0;self.attempts=0
  if not carrier:self.carrier=False;self.since=now;return None
  if not self.carrier:self.carrier=True;self.since=now
  if any(a!=LEGACY and private(a) for a in addresses):
   self.since=now;self.attempts=0;self.next_try=0;return None
  if addresses and addresses!=[LEGACY]:return None
  if addresses==[LEGACY]:
   if key[0]!='usb0' or key[2]!='Ifupdown (usb0)':return None
   if method=='manual':return 'legacy' if now>=self.next_try else None
   if method=='auto' and now-self.since>=60 and now>=self.next_try:return 'recover'
   return None
  if now-self.since<60 or now<self.next_try:return None
  if method=='manual' and key[0]=='usb0' and key[2]=='Ifupdown (usb0)':return 'legacy'
  if method=='auto':return 'recover'
  return None
 def attempted(self,now):
  self.attempts+=1;self.next_try=now+min(300,60*(2**min(self.attempts-1,3)))
 def reset(self):self.__init__()

def private(text):
 try:
  addr=ipaddress.ip_address(text)
  return addr.version==4 and any(addr in ipaddress.ip_network(x) for x in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16'))
 except ValueError:return False

def cmd(a,timeout=8):return subprocess.run(a,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=timeout)
def state(iface):
 index=Path('/sys/class/net',iface,'ifindex').read_text().strip()
 carrier=Path('/sys/class/net',iface,'carrier').read_text().strip()=='1'
 p=cmd(['/usr/bin/nmcli','-g','GENERAL.CON-UUID','device','show',iface]);a=cmd(['/usr/sbin/ip','-j','-4','address','show','dev',iface])
 if p.returncode or a.returncode:raise ValueError('device state unavailable')
 uuid=p.stdout.strip();infos=json.loads(a.stdout);addresses=[x['local'] for row in infos for x in row.get('addr_info',[]) if x.get('family')=='inet']
 dynamic_lease=any(x.get('dynamic') is True for row in infos for x in row.get('addr_info',[]) if x.get('family')=='inet')
 if not uuid or uuid=='--':
  available=cmd(['/usr/bin/nmcli','-g','CONNECTIONS.AVAILABLE-CONNECTIONS','device','show',iface])
  if available.returncode:raise ValueError('profiles unavailable')
  ids=re.findall(r'(?im)^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\s*\|',available.stdout)
  if len(ids)!=1:raise ValueError('profiles ambiguous')
  uuid=ids[0]
 if not re.fullmatch(r'[0-9a-fA-F-]{36}',uuid):raise ValueError('invalid profile uuid')
 u=cmd(['/usr/bin/nmcli','-g','connection.id,connection.type,connection.interface-name,ipv4.method','connection','show','uuid',uuid])
 if u.returncode:raise ValueError('profile unavailable')
 lines=u.stdout.splitlines()
 if len(lines)!=4 or lines[1] not in ('802-3-ethernet','ethernet') or lines[2] not in ('','--',iface):raise ValueError('profile incompatible')
 return (iface,index,lines[0],uuid),addresses,carrier,lines[3],dynamic_lease

def idle_lease():
 spec=importlib.util.spec_from_file_location('headring_capture_guard','/usr/local/lib/headring-time/headring_capture_guard.py')
 module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
 return module.CaptureGuard().lease(),module

def activate_legacy(iface,key,ips,dynamic,meta):
 modify=['/usr/bin/nmcli','device','modify',iface,'ipv4.method','auto','ipv4.addresses','','ipv4.gateway','','ipv4.dns','','ipv4.routes','','ipv4.never-default','yes','ipv4.ignore-auto-dns','yes']
 result=cmd(modify,20)
 if result.returncode and not ips:
  cur,again,still_link,_,has_lease=state(iface)
  if cur==key and still_link and not again and not has_lease and dynamic.find_interface(meta)==iface:
   result=cmd(['/usr/bin/nmcli','connection','up','uuid',key[3],'ifname',iface],20)
   if result.returncode==0:
    cur,again,still_link,_,has_lease=state(iface)
    if cur==key and still_link and not has_lease and again in ([],[LEGACY]) and dynamic.find_interface(meta)==iface:result=cmd(modify,20)
    else:raise ValueError('profile changed or lease arrived during activation')
 return result

def safe_live_reapply(decision, key, addresses, carrier, method, leased):
 """Only replace this legacy unleased phone-IP setting; never reset USB."""
 return (decision == 'legacy' and key[0] == 'usb0' and key[2] == 'Ifupdown (usb0)'
         and addresses == [LEGACY] and carrier is True and method == 'manual' and not leased)

def reapply_phone_ipv4(iface):
 return cmd(['/usr/bin/nmcli','device','modify',iface,'ipv4.method','auto','ipv4.addresses','',
             'ipv4.gateway','','ipv4.dns','','ipv4.routes','','ipv4.never-default','yes',
             'ipv4.ignore-auto-dns','yes'],20)

def run():
 spec=importlib.util.spec_from_file_location('dynamic',BASE+'/dynamic_guard.py');dynamic=importlib.util.module_from_spec(spec);spec.loader.exec_module(dynamic)
 host=dynamic.guard.Host();meta=host.load(META)
 if host.identity()!=meta['device_id']:raise SystemExit(2)
 policy=RetryPolicy()
 while True:
  try:
   iface=dynamic.find_interface(meta)
   if not iface:policy.reset();time.sleep(3);continue
   key,addresses,carrier,method,leased=state(iface);now=time.monotonic()
   if leased:policy.reset();time.sleep(3);continue
   decision=policy.observe(key,addresses,carrier,method,now)
   if decision:
    # Reapply only a stale, unleased USB network profile; no hardware reset or
    # device reconnect. This allows the operator's phone to reconnect during
    # capture while every camera and sensor remains untouched.
    if safe_live_reapply(decision,key,addresses,carrier,method,leased):
     current,ips,link,method_now,leased_now=state(iface)
     if (host.identity()==meta['device_id'] and dynamic.find_interface(meta)==iface
         and current==key and safe_live_reapply(decision,current,ips,link,method_now,leased_now)):
      policy.attempted(time.monotonic());result=reapply_phone_ipv4(iface)
      print(json.dumps({'state':'DHCP_REQUESTED' if result.returncode==0 else 'DHCP_RETRY_FAILED','interface':iface,'attempt':policy.attempts,'operation':'device_ipv4_reapply'}),flush=True)
      time.sleep(3);continue
    lease,guard=idle_lease()
    with lease:
     g=guard.CaptureGuard();ok,reason=g.allowed()
     # Require confirmed idle before reconfiguring the phone network.
     if not ok:continue
     if host.identity()!=meta['device_id'] or dynamic.find_interface(meta)!=iface:continue
     current,ips,link,method_now,leased_now=state(iface)
     if current!=key or not link or leased_now or (ips and ips!=[LEGACY]) or (decision=='recover' and method_now!='auto') or (decision=='legacy' and (method_now!='manual' or key[0]!='usb0' or key[2]!='Ifupdown (usb0)')):continue
     policy.attempted(time.monotonic())
     if decision=='legacy':result=activate_legacy(iface,key,ips,dynamic,meta)
     else:result=cmd(['/usr/bin/nmcli','connection','up','uuid',key[3],'ifname',iface],20)
     print(json.dumps({'state':'DHCP_REQUESTED' if result.returncode==0 else 'DHCP_RETRY_FAILED','interface':iface,'attempt':policy.attempts}),flush=True)
  except (OSError,ValueError,KeyError,subprocess.TimeoutExpired,ImportError):pass
  time.sleep(3)
if __name__=='__main__':run()
