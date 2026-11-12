"""Build a static CP15 candidate from immutable Git objects; never deploys."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import zipfile

HERE=Path(__file__).resolve().parent
REPO=HERE.parents[1]
BASE='f5d03b7b5fb197eb76e78610a0bce672e5cc4853'
CANDIDATE='46b6538'
STABLE_SHA='95c02f40ad634a02218df4bc84c4093c3571cae3eace0dc6c95fa0e87f462109'
OVERLAY_SHA='36d8059918ce370f7928cc7cd3207e2ac1d26471b3e8dcdef74b98b7888cd62a'
PREFIX='orangepi/src/orangepi_collect/'
RELATIVE=(
 'acquisition/eeg_bridge.py','acquisition/eeg_worker.py','common/eeg_impedance.py',
 'common/impedance_quality.py','common/impedance_record.py','common/scan_registry.py',
 'control/dashboard.html','control/dashboard.py','control/device_diagnostics.py','control/diagnostic_events.py',
 'control/launcher.py','control/phone_recording.js','control/scan_process.py','control/web_controls.py',
 'framework/framework_recorder.py',
)
ALLOWLIST=tuple(PREFIX+p for p in RELATIVE)

def digest(value):return hashlib.sha256(value).hexdigest()
def git_bytes(commit,path):return subprocess.check_output(['git','-C',str(REPO),'show',commit+':'+path])
def canonical(value):return (json.dumps(value,sort_keys=True,separators=(',',':'))+'\n').encode()

def main():
    stable_raw=(HERE/'resources/manifest.json').read_bytes()
    overlay_raw=(HERE/'resources/eeg-impedance-advisory-36d8059918ce/manifest.json').read_bytes()
    if digest(stable_raw)!=STABLE_SHA or digest(overlay_raw)!=OVERLAY_SHA:raise ValueError('baseline pin mismatch')
    stable=json.loads(stable_raw);overlay=json.loads(overlay_raw)
    if stable['commit']!=BASE or len(stable['files'])!=236 or len(overlay['files'])!=5:raise ValueError('baseline shape mismatch')
    before={x['path']:x['sha256'] for x in stable['files']}
    for item in overlay['files']:
        if before.get(item['path'])!=item['before_sha256']:raise ValueError('overlay baseline mismatch')
        before[item['path']]=item['sha256']
    before[PREFIX+'control/diagnostic_events.py']=None
    files=[];payload={}
    for path in ALLOWLIST:
        data=git_bytes(CANDIDATE,path);payload[path]=data
        if path not in before or digest(data)==before[path]:raise ValueError('allowlist path is missing or unchanged: '+path)
        files.append(dict(path=path,before_sha256=before[path],sha256=digest(data),size=len(data),git_mode='100644'))
    changed=set()
    for path,expected in before.items():
        try:data=git_bytes(CANDIDATE,path)
        except subprocess.CalledProcessError:data=None
        actual=digest(data) if data is not None else None
        if actual!=expected:changed.add(path)
    if changed!=set(ALLOWLIST):raise ValueError('candidate changes do not match exact allowlist: '+repr(sorted(changed^set(ALLOWLIST))))
    manifest=canonical(dict(schema=1,project=stable['project'],base_commit=BASE,candidate_commit=CANDIDATE,
        stable_manifest_sha256=STABLE_SHA,overlay_manifest_sha256=OVERLAY_SHA,files=files))
    manifest_sha=digest(manifest);release='cp15-start-impedance-diagnostics-'+manifest_sha[:12]
    original=(HERE/'resources/eeg-impedance-advisory-36d8059918ce/transaction.py').read_text()
    common=original.split('\nPINNED_FILES = ',1)[0]+'\n'
    tail=(HERE/'cp15_transaction_tail.py.in').read_text()
    for key,value in {'__PINNED_FILES__':repr(files),'__EXPECTED_BEFORE__':repr(before),
                      '__MANIFEST_SHA__':manifest_sha,'__CANDIDATE_COMMIT__':CANDIDATE}.items():tail=tail.replace(key,value)
    transaction=(common+tail).encode();compile(transaction,'transaction.py','exec')
    names=['manifest.json','transaction.py']+['payload/'+p for p in ALLOWLIST]
    bootstrap=(HERE/'cp15_bootstrap.py.in').read_text().replace('__NAMES__',repr(names)).encode()
    compile(bootstrap,'bootstrap.py','exec')
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as z:
        for name,data in [('manifest.json',manifest),('transaction.py',transaction)]+[('payload/'+p,payload[p]) for p in ALLOWLIST]:
            info=zipfile.ZipInfo(name,date_time=(2020,1,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED;info.external_attr=0o600<<16;z.writestr(info,data)
    directory=HERE/'resources'/release;directory.mkdir(exist_ok=True)
    for name,data in (('manifest.json',manifest),('transaction.py',transaction),('bootstrap.py',bootstrap),('bundle.zip',output.getvalue())):
        (directory/name).write_bytes(data)
    pins=dict(release=release,manifest_sha256=manifest_sha,bundle_sha256=digest(output.getvalue()),
              transaction_sha256=digest(transaction),bootstrap_sha256=digest(bootstrap),candidate=CANDIDATE,
              file_count=len(files),expected_before_count=len(before))
    (directory/'pins.json').write_bytes(canonical(pins))
    print(json.dumps(pins,indent=2,sort_keys=True))

if __name__=='__main__':main()
