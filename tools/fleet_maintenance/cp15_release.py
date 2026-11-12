"""Strict, explicitly selected CP15-only canary; no discovery or unknown baseline."""
import base64
import io
import json
import re
import shlex
import time
import zipfile

import fleet
import online
import delta_release

RELEASE='cp15-start-impedance-diagnostics-7fcb1f8dac51'
MANIFEST_SHA='7fcb1f8dac518c173eee34f8da3069bb296a8ad25290e3256f7655ef0c31e731'
BUNDLE_SHA='a310b9ec5f5e060dbb02b7210726a54d50929c41dbace8f622f7f78ce5b34ede'
TRANSACTION_SHA='7f04c444f54791dc4a49bb4bab026297a9794dbdb3cc4ab10bdd181ed3991941'
BOOTSTRAP_SHA='c9b59dd6efaf1f0e21f2b269aa4bae4260c5fd92b09dc9f804735e1d6451bb66'
OVERLAY_SHA='36d8059918ce370f7928cc7cd3207e2ac1d26471b3e8dcdef74b98b7888cd62a'
PREFIX='orangepi/src/orangepi_collect/'
PATHS=tuple(PREFIX+x for x in (
 'acquisition/eeg_bridge.py','acquisition/eeg_worker.py','common/eeg_impedance.py',
 'common/impedance_quality.py','common/impedance_record.py','common/scan_registry.py',
 'control/dashboard.html','control/dashboard.py','control/device_diagnostics.py','control/diagnostic_events.py',
 'control/launcher.py','control/phone_recording.js','control/scan_process.py','control/web_controls.py',
 'framework/framework_recorder.py'))
NEW=PREFIX+'control/diagnostic_events.py'
RESOURCE=fleet.RESOURCES/RELEASE

def registered():
    raw=(RESOURCE/'manifest.json').read_bytes();bundle=(RESOURCE/'bundle.zip').read_bytes()
    transaction=(RESOURCE/'transaction.py').read_bytes();bootstrap=(RESOURCE/'bootstrap.py').read_bytes()
    if tuple(map(fleet.sha,(raw,bundle,transaction,bootstrap)))!=(MANIFEST_SHA,BUNDLE_SHA,TRANSACTION_SHA,BOOTSTRAP_SHA):
        raise online.Failure('CP15 resource hash mismatch')
    stable_raw=(fleet.RESOURCES/'manifest.json').read_bytes()
    overlay_raw=(delta_release.RESOURCE/'manifest.json').read_bytes()
    if fleet.sha(stable_raw)!=fleet.MANIFEST_SHA256 or fleet.sha(overlay_raw)!=OVERLAY_SHA:
        raise online.Failure('reviewed baseline resource hash mismatch')
    stable=json.loads(stable_raw);overlay=json.loads(overlay_raw);manifest=json.loads(raw)
    if stable.get('commit')!=fleet.PIN or len(stable.get('files',[]))!=236 or len(overlay.get('files',[]))!=5:
        raise online.Failure('baseline shape mismatch')
    before={i['path']:i['sha256'] for i in stable['files']}
    for item in overlay['files']:
        if before.get(item['path'])!=item['before_sha256']:raise online.Failure('overlay baseline mismatch')
        before[item['path']]=item['sha256']
    before[NEW]=None
    items=manifest.get('files',[])
    if (manifest.get('base_commit')!=fleet.PIN or manifest.get('candidate_commit')!='46b6538'
            or manifest.get('project')!=online.PROJECT or [i['path'] for i in items]!=list(PATHS)
            or len(before)!=237 or len(items)!=15):raise online.Failure('CP15 manifest identity/allowlist mismatch')
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        names=archive.namelist();expected={'manifest.json','transaction.py'}|{'payload/'+p for p in PATHS}
        if len(names)!=len(expected) or set(names)!=expected or archive.read('manifest.json')!=raw or archive.read('transaction.py')!=transaction:
            raise online.Failure('CP15 archive members mismatch')
        for item in items:
            path=item['path'];data=archive.read('payload/'+path)
            if (item.get('before_sha256')!=before.get(path) or item.get('git_mode')!='100644'
                    or item.get('size')!=len(data) or fleet.sha(data)!=item.get('sha256')):
                raise online.Failure('CP15 payload mismatch: '+path)
    requested=dict(stable);requested['files']=[{'path':p} for p in before]
    requested['python_modules']=list(dict.fromkeys(stable.get('python_modules',[])+['diagnostic_events']))
    return manifest,requested,before,bundle,bootstrap

def classify(live,manifest,before):
    files=live.get('files') or {};after=dict(before)
    after.update({item['path']:item['sha256'] for item in manifest['files']})
    def matches(expected):
        for path,sha in expected.items():
            state=files.get(path) or {}
            parts=path.split('/')[:-1];parents=state.get('parents')
            expected_parents=['/'.join(parts[:i]) for i in range(1,len(parts)+1)]
            if (not isinstance(parents,list) or len(parents)!=len(expected_parents)
                    or any(not isinstance(p,dict) or p.get('type')!='dir' or p.get('path')!=name
                           for p,name in zip(parents,expected_parents))):return False
            if sha is None:
                if state.get('type')!='absent':return False
            elif state.get('type')!='regular' or state.get('sha256')!=sha:return False
        return True
    if matches(before):return 'before'
    if matches(after):return 'after'
    raise online.Failure('unknown, mixed, missing or unsafe 237-file baseline')

def stage_apply(ssh,bundle,bootstrap,command_file=None):
    if command_file is None:ssh.require_tty()
    stage=ssh.run('umask 077; mktemp -d "$HOME/.headring-stage-XXXXXXXX"').decode().strip()
    if not re.fullmatch(r'/home/orangepi/\.headring-stage-[A-Za-z0-9]{8}',stage):raise online.Failure('unsafe stage path')
    path=stage+'/bundle.zip';ssh.run('umask 077; cat > '+shlex.quote(path),bundle)
    root='/var/lib/headring-cp15-canary/'+MANIFEST_SHA
    code='import base64;exec(compile(base64.b64decode('+repr(base64.b64encode(bootstrap).decode())+'),"bootstrap","exec"))'
    boot='python3 -c '+shlex.quote(code)+' '+shlex.quote(path)+' '+BUNDLE_SHA+' '+MANIFEST_SHA
    command='python3 '+shlex.quote(root+'/transaction.py')+' --device cp_0015 --manifest-sha256 '+MANIFEST_SHA
    dry='sudo sh -c '+shlex.quote('set -eu; '+boot+'; '+command)
    apply='sudo sh -c '+shlex.quote('set -eu; '+command+' --apply')
    if command_file:
        import os
        content={'device':'cp_0015','stage':stage,'manifest_sha256':MANIFEST_SHA,
            'dry_run_argv':ssh.argv[:-1]+['-T',ssh.argv[-1],online.sudo_stdin_command(dry)],
            'apply_argv':ssh.argv[:-1]+['-T',ssh.argv[-1],online.sudo_stdin_command(apply)],
            'warning':'Fresh idle and independent review required; no credentials stored.'}
        fd=os.open(command_file,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as f:json.dump(content,f,indent=2,sort_keys=True);f.write('\n')
        return {'status':'staged_not_applied','command_file':str(command_file)}
    ssh.run('sudo sh -c '+shlex.quote('set -eu; '+boot+'; '+command+'; '+command+' --apply'),interactive=True)
    return {'status':'applied','root_bundle':root}

def main(args,parser):
    if args.release!=RELEASE:parser.error('unknown CP15 release')
    if args.targets!=['cp_0015']:parser.error('this release requires exactly cp_0015')
    if args.approve_unknown_baseline or args.audit_recordings or args.local_root:parser.error('unknown baseline and recording audit forbidden')
    if args.apply and args.verify:parser.error('--apply already verifies')
    if (args.apply or args.verify) and not args.accepted_plan:parser.error('accepted plan required')
    if args.offline_plan and (args.apply or args.verify or args.accepted_plan):parser.error('offline plan cannot deploy')
    addresses=dict(args.address)
    if addresses.get('cp_0015','192.168.20.109')!='192.168.20.109':parser.error('CP15 cached address mismatch')
    if args.bind_interface not in (None,'wlx40a5ef5ff812') or args.bind_address not in (None,'192.168.20.100'):
        parser.error('CP15 wireless binding mismatch')
    manifest,requested,before,bundle,bootstrap=registered()
    if args.offline_plan:
        print(json.dumps({'release':RELEASE,'manifest_sha256':MANIFEST_SHA,'bundle_sha256':BUNDLE_SHA,
            'transaction_sha256':TRANSACTION_SHA,'bootstrap_sha256':BOOTSTRAP_SHA,'baseline_files':237,
            'payload_files':15,'status':'offline_manifest_validated','deploy_ready':False},indent=2));return 0
    try:
        values=online.config_for(args.ssh_config,'cp_0015');alias=values.get('hostkeyalias') or values.get('hostname')
        if alias!='192.168.1.119':raise online.Failure('CP15 host key alias mismatch')
        ssh=online.SSH(args.ssh_config,'cp_0015','192.168.20.109','wlx40a5ef5ff812','192.168.20.100')
        live=online.read_live(ssh,requested);online.gates(live,'cp_0015')
        state=classify(live,manifest,before);profile=delta_release.read_profile(ssh)
        if profile.get('policy')!='advisory':raise online.Failure('CP15 EEG policy is not advisory')
        if args.apply or args.verify:
            plan=online.load_plan(args.state_dir,args.accepted_plan)
            if (plan.get('release')!=RELEASE or plan.get('device')!='cp_0015' or plan.get('manifest_sha256')!=MANIFEST_SHA
                    or plan.get('address')!='192.168.20.109' or plan.get('hostkey_alias')!=alias):raise online.Failure('accepted plan identity mismatch')
            if time.time()-plan.get('created_at',0)>600 or time.time()<plan.get('created_at',0)-30:raise online.Failure('plan expired')
            if any(live.get(k)!=plan.get(k) for k in ('files','protected','camd_pid')) or profile!=plan.get('profile') or state!=plan.get('baseline'):
                raise online.Failure('live state drifted since plan')
            if state=='after':result={'status':'already_installed_verified','root_mutation':False}
            elif args.verify:raise online.Failure('candidate not installed')
            else:
                result=stage_apply(ssh,bundle,bootstrap,args.sudo_command_file)
                if result['status']=='applied':
                    after=online.read_live(ssh,requested);online.gates(after,'cp_0015')
                    if classify(after,manifest,before)!='after' or after['protected']!=plan['protected'] or after['camd_pid']!=plan['camd_pid']:
                        raise online.Failure('post-apply verification failed; inspect transaction receipt')
                    if delta_release.read_profile(ssh)!=profile:raise online.Failure('EEG profile changed')
                    result['verified_files']=237
        else:
            plan={'schema':1,'release':RELEASE,'device':'cp_0015','address':'192.168.20.109','hostkey_alias':alias,'manifest_sha256':MANIFEST_SHA,
                'created_at':time.time(),'baseline':state,'files':live['files'],'protected':live['protected'],'camd_pid':live['camd_pid'],'profile':profile}
            digest,path=online.write_plan(args.state_dir,plan)
            result={'status':'plan_ready','plan_sha256':digest,'plan_path':str(path),'baseline':state,'deploy_ready':False}
        print(json.dumps({'device':'cp_0015','release':RELEASE,**result},indent=2,sort_keys=True))
        return 3 if result.get('status')=='staged_not_applied' else 0
    except (online.Failure,OSError,ValueError,KeyError) as exc:
        print(json.dumps({'device':'cp_0015','release':RELEASE,'status':'offline_or_rejected','error':str(exc)},indent=2));return 2
