#!/usr/bin/env python3
"""Build a self-contained offline payload without executing package scripts."""
import argparse, hashlib, io, json, os, re, subprocess, tarfile, tempfile
from pathlib import Path

BASELINE='083155214907c55dc61d78fb34d65b29d12f2b82'
BASE_COLLECTOR='f5d03b7b5fb197eb76e78610a0bce672e5cc4853'
OVERLAY_MANIFEST='1fdd00add147d81c230293f6dc5221f77ff2c37b26d6cd301a09310d7f0a2fac'
LEGACY='460c04cb719aca5d73bfe518748bd636c632e2cf'
PREIMAGES={'7004315fbc567afc5f0d5e9fcd0552f0db46a9c8':'d636ca9867d09c4d1f57436cb7a8044d171e8b201d0bf1ec1eb0b9b4acb7dc21','460c04cb719aca5d73bfe518748bd636c632e2cf':'be3f2734c80c6f1df24de7b4b4c001ff749c0baa2b064f49aa57387b736de3ea','8c72ed375c33b39d15e00dc9ee9d37c82b54e6b3':'bd626687ef27c70d6a6cf6ab83d5567da147af540e6a8e33877a5da06c939aa3'}
ROOT=Path(__file__).resolve().parents[2]
FEATURE_SOURCES={
 'support/usb_export_mount.py':'orangepi/scripts/usb_export_mount.py',
 'support/usb_tasks_mount_readonly.py':'orangepi/scripts/usb_tasks_mount_readonly.py',
 'support/headring-phone-time':'services/phone-time/headring-phone-time',
 'support/headring-lan-time':'services/phone-time/headring-lan-time',
 'support/headring_capture_guard.py':'services/phone-time/headring_capture_guard.py'}
PKGS={
 'avahi-daemon_0.7-4ubuntu7.3_arm64.deb':('d903ad01bbef69380d77b8c9445a54b23861accd935254a38160d09cd851239f','https://ports.ubuntu.com/ubuntu-ports/pool/main/a/avahi/avahi-daemon_0.7-4ubuntu7.3_arm64.deb'),
 'libavahi-core7_0.7-4ubuntu7.3_arm64.deb':('46c94c47184e5942e18596595d410939d2537e985a3acb1268c037e06868f028','https://ports.ubuntu.com/ubuntu-ports/pool/main/a/avahi/libavahi-core7_0.7-4ubuntu7.3_arm64.deb')}

def digest(data): return hashlib.sha256(data).hexdigest()
def git(*args):return subprocess.check_output(['git','-C',str(ROOT)]+list(args))
def collector_sources():
 """Use a frozen, hashed release overlay, never silently ship old Git bytes."""
 profile_path=ROOT/'deployment/phone-usb-entry/collector-release.json'
 if profile_path.is_symlink():raise ValueError('release profile symlink')
 raw=profile_path.read_bytes();profile=json.loads(raw)
 if (profile.get('schema')!=1 or profile.get('baseline_commit')!=BASELINE
     or profile.get('rotation')!={'block_size':5,'count_basis':'saved','progress_version':4,'preserve_existing_n':True}):raise ValueError('invalid collector release profile')
 paths=git('ls-tree','-r','--name-only',BASELINE,'orangepi/src/orangepi_collect').decode().splitlines()
 paths=sorted(p for p in paths if p.endswith(('.py','.js','.html','.css')))
 if len(paths)!=123:raise ValueError('unexpected baseline source count')
 sources={p:git('show',BASELINE+':'+p) for p in paths}
 overrides=profile.get('overrides')
 if not isinstance(overrides,dict) or not overrides:raise ValueError('saved rotation overrides missing')
 for path,entry in overrides.items():
  if (set(entry)!={'pre_sha256','post_sha256','size'} or not isinstance(path,str)
      or not re.fullmatch(r'orangepi/src/orangepi_collect/[A-Za-z0-9_./-]+\.(?:py|js|html|css)',path)
      or any(part in ('','.','..') for part in path.split('/'))):raise ValueError('invalid source override')
  target=profile_path.parent/'collector-overrides'/path
  if any(p.is_symlink() for p in [target]+list(target.parents)):raise ValueError('source override symlink')
  data=target.read_bytes();before=digest(sources[path]) if path in sources else None
  if (len(data)>8_000_000 or before!=entry['pre_sha256']
      or digest(data)!=entry['post_sha256'] or len(data)!=entry['size']):raise ValueError('collector override mismatch: '+path)
  sources[path]=data
 if not 123<=len(sources)<=155:raise ValueError('unexpected collector source count')
 index={p:{'sha256':digest(data),'size':len(data)} for p,data in sources.items()}
 return sources,profile,digest(json.dumps(index,sort_keys=True,separators=(',',':')).encode())
def read_deb(path,expected):
 raw=path.read_bytes()
 if digest(raw)!=expected:raise ValueError('dependency hash mismatch: '+path.name)
 rawtar=subprocess.check_output(['dpkg-deb','--fsys-tarfile',str(path)],timeout=30)
 with tarfile.open(fileobj=io.BytesIO(rawtar),mode='r:') as t:
  found={}
  for m in t:
   name=m.name.removeprefix('./')
   if name in ('usr/sbin/avahi-daemon','usr/lib/aarch64-linux-gnu/libavahi-core.so.7.0.2','usr/share/doc/avahi-daemon/copyright','usr/share/doc/libavahi-core7/copyright'):
    if name in found or not m.isfile() or m.size>4_000_000:raise ValueError('unsafe dependency member')
    found[name]=t.extractfile(m).read()
  return found

def main():
 p=argparse.ArgumentParser();p.add_argument('--device',required=True);p.add_argument('--from-commit',choices=tuple(PREIMAGES));p.add_argument('--usb-port');p.add_argument('--unique-usb-network',action='store_true');p.add_argument('--deps',type=Path,required=True);p.add_argument('--apk',type=Path,required=True);p.add_argument('--signer-sha256',required=True);p.add_argument('--android-sdk',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
 if not re.fullmatch(r'cp_[0-9]{4}',a.device) or (bool(a.usb_port) and a.unique_usb_network) or (a.usb_port and not re.fullmatch(r'[1-9][0-9]*-[1-9][0-9]*(?:\.[1-9][0-9]*)*',a.usb_port)):p.error('provide exactly one valid USB selector')
 selector={'drivers':['cdc_ether','cdc_ncm','rndis_host']}
 if a.usb_port:selector['usb_port']=a.usb_port
 else:selector['mode']='unique-usb-network'
 apk=a.apk.read_bytes();tool=a.android_sdk/'build-tools/35.0.0';verify=subprocess.run([str(tool/'apksigner'),'verify','--print-certs',str(a.apk)],capture_output=True,text=True,check=True).stdout
 cert=re.search(r'Signer #1 certificate SHA-256 digest: ([0-9a-f]{64})',verify)
 if not cert or not re.fullmatch(r'[0-9a-f]{64}',a.signer_sha256) or cert.group(1)!=a.signer_sha256:raise ValueError('APK signer mismatch')
 xml=subprocess.check_output([str(tool/'aapt2'),'dump','xmltree',str(a.apk),'--file','AndroidManifest.xml'],text=True)
 badging=subprocess.check_output([str(tool/'aapt2'),'dump','badging',str(a.apk)],text=True)
 if not re.search(r"^package: name='org\.headring\.usbentry(?:\.v5)?' versionCode='([4-9]|[1-9][0-9]+)'",badging,re.M):raise ValueError('unexpected APK package/version')
 entries=re.findall(r'E: meta-data \(line=[^\n]*\)(.*?)(?=\n\s*E:|\Z)',xml,re.S)
 if not any('org.headring.PROTOCOL' in x and 'headring-usb-v1' in x for x in entries):raise ValueError('APK is not the universal protocol version')
 blobs={};deps={}
 for name,(sha,url) in PKGS.items():deps[name]={'sha256':sha,'url':url};blobs.update(read_deb(a.deps/name,sha))
 expected=['usr/sbin/avahi-daemon','usr/lib/aarch64-linux-gnu/libavahi-core.so.7.0.2','usr/share/doc/avahi-daemon/copyright','usr/share/doc/libavahi-core7/copyright']
 if set(blobs)!=set(expected):raise ValueError('dependency members missing')
 runtime_dir=ROOT/'services/phone-usb-helper/device'
 sources,collector_profile,collector_sha=collector_sources()
 seed={'device':a.device,'from_commit':a.from_commit,'selector':selector,'apk':digest(apk),'baseline':BASELINE,'builder':digest(Path(__file__).read_bytes()),'installer':digest((ROOT/'deployment/phone-usb-entry/deploy_bundle.py').read_bytes()),'dependencies':{k:v[0] for k,v in PKGS.items()},'runtime':{k:digest((runtime_dir/k).read_bytes()) for k in ('guard.py','dynamic_guard.py','probe.py','usb_dhcp_guard.py')},'collector_source_sha256':collector_sha,'rotation':collector_profile['rotation']}
 seed['integrated_helpers']={target:digest((ROOT/source).read_bytes()) for target,source in FEATURE_SOURCES.items()}
 rid='offline-'+digest(json.dumps(seed,sort_keys=True).encode())[:16];prefix='/opt/headring-offline/releases/'+rid
 files={'release/avahi-daemon':blobs[expected[0]],'release/lib/libavahi-core.so.7':blobs[expected[1]],'release/licenses/avahi-daemon.copyright':blobs[expected[2]],'release/licenses/libavahi-core7.copyright':blobs[expected[3]],'release/headring-helper.apk':apk}
 device_dir=ROOT/'services/phone-usb-helper/device'
 for name in ('guard.py','dynamic_guard.py','probe.py','usb_dhcp_guard.py'):files['release/'+name]=(device_dir/name).read_bytes()
 cfg=('[server]\nhost-name=cp-'+a.device[3:]+'\ndomain-name=local\nuse-ipv4=yes\nuse-ipv6=no\nallow-interfaces=usb1\nallow-point-to-point=no\nuse-iff-running=yes\ncheck-response-ttl=yes\ndisallow-other-stacks=yes\nenable-dbus=no\nratelimit-interval-usec=1000000\nratelimit-burst=100\n[wide-area]\nenable-wide-area=no\n[publish]\npublish-addresses=yes\npublish-hinfo=no\npublish-workstation=no\npublish-domain=no\npublish-resolv-conf-dns-servers=no\npublish-aaaa-on-ipv4=no\npublish-a-on-ipv6=no\ndisable-user-service-publishing=yes\n[reflector]\nenable-reflector=no\n').encode()
 files['release/etc-avahi/avahi-daemon.conf']=cfg;files['release/etc-avahi/hosts']=b''
 name='HeadRing CP'+str(int(a.device[3:]));files['release/etc-avahi/services/headring.service']=('<?xml version="1.0" standalone="no"?>\n<!DOCTYPE service-group SYSTEM "avahi-service.dtd">\n<service-group><name>'+name+'</name><service><type>_headring._tcp</type><port>8765</port><txt-record>device_id='+a.device+'</txt-record><txt-record>path=/phone</txt-record><txt-record>scheme=http</txt-record></service></service-group>\n').encode()
 meta={'schema':1,'device_id':a.device,'interface':'usb1','selector':selector,'binary_path':prefix+'/avahi-daemon','binary_sha256':digest(files['release/avahi-daemon']),'config_path':prefix+'/etc-avahi/avahi-daemon.conf','config_sha256':digest(cfg)}
 files['release/metadata.json']=(json.dumps(meta,sort_keys=True,indent=2)+'\n').encode()
 paths=sorted(sources)
 additions=sorted(path for path,entry in collector_profile['overrides'].items() if entry['pre_sha256'] is None)
 preimages=None
 if a.from_commit:
  old=git('ls-tree','-r','--name-only',a.from_commit,'orangepi/src/orangepi_collect').decode().splitlines();old={x for x in old if x.endswith(('.py','.js','.html','.css'))}
  if len(old)!={'460c04cb719aca5d73bfe518748bd636c632e2cf':111,'8c72ed375c33b39d15e00dc9ee9d37c82b54e6b3':121,'7004315fbc567afc5f0d5e9fcd0552f0db46a9c8':123}[a.from_commit] or not old.issubset(set(paths)):raise ValueError('legacy source list mismatch')
  preimages={path:(digest(git('show',a.from_commit+':'+path)) if path in old else None) for path in sorted(paths)}
  legacy_preimages={path:value for path,value in preimages.items() if path not in additions}
  if digest(json.dumps(legacy_preimages,sort_keys=True,separators=(',',':')).encode())!=PREIMAGES[a.from_commit]:raise ValueError('legacy source lock mismatch')
 for path in paths:files['collector/'+path]=sources[path]
 for path in ('orangepi/scripts/run.sh','services/usb-phone-link/usb_data_host.py'):files['support/'+path]=git('show',BASELINE+':'+path)
 for target,source in FEATURE_SOURCES.items():files[target]=(ROOT/source).read_bytes()
 files['support/deploy_bundle.py']=(ROOT/'deployment/phone-usb-entry/deploy_bundle.py').read_bytes()
 files['support/import_text_tasks.py']=git('show',BASELINE+':tools/import_text_tasks.py')
 modes={path:(0o755 if path=='release/avahi-daemon' else 0o644) for path in files}
 manifest={'schema':1,'baseline_commit':BASELINE,'base_collector_commit':BASE_COLLECTOR,'collector_overlay_manifest_sha256':OVERLAY_MANIFEST,'device_id':a.device,'selector':meta['selector'],'release_id':rid,'release_prefix':prefix,'apk_sha256':digest(apk),'apk_certificate_sha256':cert.group(1),'dependencies':deps,'files':{k:{'sha256':digest(v),'size':len(v),'mode':modes[k]} for k,v in sorted(files.items())}}
 manifest.update(collector_source_sha256=collector_sha,task_rotation=collector_profile['rotation'],collector_additions=additions,integrated_helpers_version=1)
 if preimages is not None:manifest['source_preimage_commit']=a.from_commit;manifest['source_preimage_sha256']=PREIMAGES[a.from_commit];manifest['source_preimages']=preimages
 files['manifest.json']=(json.dumps(manifest,sort_keys=True,indent=2)+'\n').encode()
 a.output.parent.mkdir(parents=True,exist_ok=True)
 with tarfile.open(a.output,'w') as t:
  for path,data in sorted(files.items()):
   info=tarfile.TarInfo(path);info.size=len(data);info.mode=0o644;info.mtime=0;info.uid=info.gid=0;info.uname=info.gname='';t.addfile(info,io.BytesIO(data))
 print(json.dumps({'bundle':str(a.output),'sha256':digest(a.output.read_bytes()),'release_id':rid,'device_id':a.device,'collector_files':len(paths)}))
if __name__=='__main__':main()
