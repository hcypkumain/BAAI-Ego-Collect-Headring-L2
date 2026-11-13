#!/usr/bin/python3
"""Repair the old usb0 gadget profile only when usb0 is the selected phone."""
import importlib.util
import json
from pathlib import Path
import subprocess
import time

BASE='/opt/headring-usb-entry-persistent'
META='/opt/headring-usb-entry-proof/proof-20261113-b524dab0/metadata.json'
spec=importlib.util.spec_from_file_location('dynamic',BASE+'/dynamic_guard.py')
dynamic=importlib.util.module_from_spec(spec);spec.loader.exec_module(dynamic)
meta=dynamic.guard.Host().load(META)
if dynamic.guard.Host().identity()!='cp_0021' or meta['device_id']!='cp_0021':raise SystemExit(2)
attempted=None; retry_after=0

def cmd(a,timeout=8):
 return subprocess.run(a,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=timeout)

while True:
 try:
  iface=dynamic.find_interface(meta)
  if iface!='usb0':
   attempted=None;time.sleep(2);continue
  ifindex=Path('/sys/class/net/usb0/ifindex').read_text().strip()
  addr=cmd(['/usr/sbin/ip','-j','-4','address','show','dev','usb0'])
  profile=cmd(['/usr/bin/nmcli','-g','GENERAL.CONNECTION','device','show','usb0'])
  if addr.returncode or profile.returncode:time.sleep(3);continue
  addresses=[x['local'] for r in json.loads(addr.stdout) for x in r.get('addr_info',[]) if x.get('family')=='inet']
  if (ifindex!=attempted and time.monotonic()>=retry_after and profile.stdout.strip()=='Ifupdown (usb0)' and addresses==['10.42.0.2']):
   if dynamic.guard.Host().identity()!='cp_0021' or dynamic.find_interface(meta)!='usb0' or Path('/sys/class/net/usb0/ifindex').read_text().strip()!=ifindex:continue
   result=cmd(['/usr/bin/nmcli','device','modify','usb0','ipv4.method','auto','ipv4.addresses','','ipv4.gateway','','ipv4.dns','','ipv4.routes','','ipv4.never-default','yes','ipv4.ignore-auto-dns','yes'],20)
   if result.returncode==0:
    attempted=ifindex;print(json.dumps({'state':'DHCP_ENABLED','interface':'usb0','ifindex':ifindex}),flush=True)
   else:
    retry_after=time.monotonic()+30;print(json.dumps({'state':'DHCP_REAPPLY_FAILED','exit':result.returncode}),flush=True)
 except (OSError,ValueError,KeyError,subprocess.TimeoutExpired):
  retry_after=time.monotonic()+30
 time.sleep(3)
