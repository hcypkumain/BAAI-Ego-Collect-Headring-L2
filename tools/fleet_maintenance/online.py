"""SSH transport and code/USB transaction orchestration."""
import base64
import getpass
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import uuid
import warnings

import fleet

PROJECT = '/home/orangepi/Desktop/Embodied-Brain-Collect-main.2'
TOOL = fleet.RESOURCES / 'remote_transaction.py'
PREFLIGHT = fleet.RESOURCES / 'preflight.py'
COMPONENT_TOOL = fleet.RESOURCES / 'remote_components.py'

class Failure(RuntimeError):
    pass

SUDO_PROMPT = '__HEADRING_FLEET_SUDO_AUTH__'

def sudo_stdin_command(command):
    """Only convert the exact, internally constructed sudo prefix."""
    if not command.startswith('sudo ') or '\n' in command or '\r' in command:
        raise Failure('interactive root command must begin with sudo and contain no newline')
    return 'sudo -S -p ' + shlex.quote(SUDO_PROMPT) + ' ' + command[len('sudo '):]

def interactive_ssh(argv):
    """Use a local PTY for prompts, while the remote SSH session has no PTY."""
    try:
        import pexpect
    except ImportError as exc:
        raise Failure('interactive execution requires the system python3-pexpect package') from exc
    child=pexpect.spawn(argv[0],argv[1:],encoding='utf-8',codec_errors='replace',echo=False,timeout=None)
    child.logfile=None
    used=set(); secrets=[]
    patterns=[re.escape(SUDO_PROMPT),r"(?i)(?:[^\r\n]*@[^\r\n]*'s\s+)?password:\s*",
              r"(?i)enter passphrase for key [^\r\n]*:\s*",r'(?i)permission denied',pexpect.EOF]
    def display(text):
        if not text: return
        for secret in secrets:
            if secret: text=text.replace(secret,'[redacted]')
        sys.stdout.write(text); sys.stdout.flush()
    try:
        while True:
            outcome=child.expect(patterns)
            display(child.before)
            if outcome==4:
                child.close()
                if child.exitstatus!=0:
                    raise Failure('interactive SSH/sudo failed (exit %s, signal %s); inspect remote receipt before retrying' % (child.exitstatus,child.signalstatus))
                return
            if outcome==3: raise Failure('SSH authentication denied; no automatic retry')
            kind='sudo' if outcome==0 else 'ssh'
            if kind in used: raise Failure(kind+' authentication prompted more than once; stopped')
            used.add(kind)
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter('error',getpass.GetPassWarning)
                    secret=getpass.getpass(('Remote sudo password: ' if kind=='sudo' else 'SSH password or key passphrase: '))
            except (EOFError,KeyboardInterrupt,getpass.GetPassWarning) as exc: raise Failure('authentication cancelled or secure terminal input unavailable') from exc
            if not secret: raise Failure('empty authentication input; stopped')
            secrets.append(secret)
            child.sendline(secret)
    finally:
        if child.isalive(): child.close(force=True)

def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False) + '\n').encode()

def config_for(config, device):
    proc = subprocess.run(['ssh', '-G', '-F', str(config), device], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode:
        raise Failure('cannot parse SSH configuration for ' + device)
    values = {}
    for line in proc.stdout.decode().splitlines():
        key, _, value = line.partition(' ')
        values.setdefault(key, value)
    if values.get('user') != 'orangepi':
        raise Failure('SSH configuration must select user orangepi for ' + device)
    return values

class SSH:
    def __init__(self, config, device, address=None, bind_interface=None, bind_address=None):
        values = config_for(config, device)
        self.device = device
        self.argv = ['ssh', '-F', str(config), '-o', 'StrictHostKeyChecking=yes',
                     '-o', 'ConnectTimeout=8', '-o', 'NumberOfPasswordPrompts=1']
        if address:
            original = values.get('hostkeyalias') or values.get('hostname')
            if not original:
                raise Failure('missing original SSH host key identity')
            self.argv += ['-o', 'HostName=' + address, '-o', 'HostKeyAlias=' + original]
        if bind_interface:
            if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,32}', bind_interface): raise Failure('invalid bind interface')
            self.argv += ['-o', 'BindInterface=' + bind_interface]
        if bind_address:
            import ipaddress
            self.argv += ['-o', 'BindAddress=' + str(ipaddress.ip_address(bind_address))]
        self.argv += [device]

    def require_tty(self):
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise Failure('interactive TTY required; run from a terminal or use --sudo-command-file for reviewed manual execution')

    def run(self, command, data=None, interactive=False):
        argv = self.argv[:-1] + ['-T'] + [self.argv[-1], sudo_stdin_command(command) if interactive else command]
        if interactive:
            self.require_tty()
            interactive_ssh(argv)
            return b''
        proc = subprocess.run(argv, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        if proc.returncode:
            raise Failure('deviceunreachable_or_ssh_failed %s: %s' % (self.device, proc.stderr.decode(errors='replace')[-500:]))
        return proc.stdout

def read_live(ssh, manifest):
    spec = importlib.util.spec_from_file_location('preflight_resource', PREFLIGHT)
    preflight = importlib.util.module_from_spec(spec); spec.loader.exec_module(preflight)
    request = {'project': PROJECT, 'paths': [i['path'] for i in manifest['files']],
               'modules': manifest.get('python_modules', []), 'source_targets': [i['path'] for i in manifest['files']]}
    # Entire read-only program and request travel as command arguments, never secrets.
    code = ('import base64,io,sys,json,contextlib,os,stat,hashlib,subprocess\n'
            'sys.stdin=io.StringIO(base64.b64decode(%r).decode())\n'
            'out=io.StringIO()\n'
            'with contextlib.redirect_stdout(out): exec(compile(base64.b64decode(%r),"preflight","exec"),{})\n'
            'd=json.loads(out.getvalue())\n'
            'ns={"__name__":"fleet_remote_read"}\n'
            'exec(compile(base64.b64decode(%r),"transaction","exec"),ns)\n'
            'd["protected"]=ns["protected_snapshot"]()\n'
            'd["system_components"]={}\n'
            'for p in %r:\n'
            ' try:\n'
            '  s=os.lstat(p)\n'
            '  d["system_components"][p]={"type":"regular" if stat.S_ISREG(s.st_mode) else "unsafe","mode":stat.S_IMODE(s.st_mode),"uid":s.st_uid,"gid":s.st_gid,"sha256":hashlib.sha256(open(p,"rb").read()).hexdigest() if stat.S_ISREG(s.st_mode) else None}\n'
            ' except FileNotFoundError: d["system_components"][p]={"type":"absent"}\n'
            ' except OSError as e: d["system_components"][p]={"type":"unknown","error":type(e).__name__}\n'
            'd["component_timer"]={}\n'
            'for op in ("is-enabled","is-active"):\n'
            ' q=subprocess.run(["systemctl",op,"collector-usb-task-mount.timer"],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True)\n'
            ' d["component_timer"][op]=q.stdout.strip()\n'
            'if not all(d["component_timer"].values()):\n'
            ' q=subprocess.run(["systemctl","show","collector-usb-task-mount.timer","-p","LoadState","--value"],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True)\n'
            ' if q.stdout.strip()=="not-found":\n'
            '  d["component_timer"]["is-enabled"]=d["component_timer"]["is-enabled"] or "not-found"\n'
            '  d["component_timer"]["is-active"]=d["component_timer"]["is-active"] or "inactive"\n'
            'print(json.dumps(d,sort_keys=True))\n') % (*tuple(base64.b64encode(x).decode() for x in (canonical(request), preflight.REMOTE.encode(), TOOL.read_bytes())), list(fleet.COMPONENTS.values()))
    out = ssh.run('python3 -c ' + shlex.quote(code))
    try: return json.loads(out)
    except (ValueError, UnicodeDecodeError) as exc: raise Failure('invalid remote preflight output') from exc

def gates(live, device):
    if live.get('device_id') != device or live.get('project') != PROJECT: raise Failure('device identity mismatch')
    if live.get('errors'): raise Failure('remote preflight errors: ' + repr(live['errors']))
    api = live.get('api') or {}; privacy = api.get('privacy') or {}
    if (api.get('state') not in ('device_confirm','idle','failed_wait','exhausted') or api.get('busy') is not False
        or api.get('session') is not None or privacy.get('required') is not False or privacy.get('phase') != 'idle'
        or privacy.get('stop_confirmed') is not True or privacy.get('session_id')): raise Failure('controller busy or unknown')
    if any(live.get('services', {}).get(n) != 'active' for n in ('embodied-pi-collect','camd')): raise Failure('service inactive or unknown')
    if live.get('capture_workers') or not live.get('capture_locks') or any(x.get('result') != 'free' for x in live['capture_locks']):
        raise Failure('capture worker or lock busy/unknown')
    if not live.get('protected'): raise Failure('protected snapshot missing')
    for module,hits in (live.get('python_module_shadow_candidates') or {}).items():
        if module != 'reference_credentials' or any(x.get('path') != 'orangepi/src/orangepi_collect/control/reference_credentials.py' for x in hits):
            raise Failure('unexpected active code shadow: ' + module)

def historical(path, digest):
    commits = fleet.git('log', '--format=%H', fleet.PIN, '--', path).decode().splitlines()
    for commit in commits:
        try: data = fleet.git('show', commit + ':' + path)
        except subprocess.CalledProcessError: continue
        if fleet.sha(data) == digest: return True
    return False

def component_diff(live, bundle):
    observed=live.get('system_components') or {}; results=[]
    for source, expected in (bundle.get('components') or {}).items():
        path=expected['destination']; before=observed.get(path,{'type':'unknown'})
        if before.get('type')=='absent': status='absent'
        elif before.get('type')!='regular': status='unknown'
        elif (before.get('sha256')==expected['sha256'] and before.get('mode')==expected['mode']
              and before.get('uid')==0 and before.get('gid')==0): status='same'
        else: status='different'
        results.append({'source':source,'path':path,'status':status,'before':before,'expected':expected})
    return results

def prepare_plan(live, manifest, device, bundle, approve_unknown=False):
    gates(live, device)
    import delta_release
    delta_release.refuse_stable_overlay(live)
    diffs=[]; baseline=[]; unknown=[]
    files=live.get('files') or {}
    for item in manifest['files']:
        path=item['path']; old=files.get(path) or {}; kind=old.get('type')
        if kind not in ('absent','regular'): raise Failure('unsafe source state: ' + path)
        digest=old.get('sha256')
        if kind == 'regular' and not re.fullmatch('[a-f0-9]{64}', digest or ''): raise Failure('missing source hash: ' + path)
        baseline.append({'path':path, 'exists':kind=='regular', 'symlink':False, 'sha256':digest})
        if digest != item['sha256']:
            known = kind == 'absent' or historical(path, digest)
            diffs.append({'path':path,'before':digest,'after':item['sha256'],'known_git_history':known})
            if not known: unknown.append(path)
    if unknown and not approve_unknown: raise Failure('unrecognized source modifications (review before --approve-unknown-baseline): ' + json.dumps([x for x in diffs if not x['known_git_history']],sort_keys=True))
    return {'schema':1,'device':device,'commit':fleet.PIN,'manifest_sha256':bundle['manifest_sha256'],
            'tool_sha256':fleet.sha(TOOL.read_bytes()), 'files':files,'baseline':baseline,'diff':diffs,
            'protected':live['protected'],'camd_pid':live.get('camd_pid'),'unknown_approved':bool(unknown),
            'component_diff':component_diff(live,bundle),'component_tool_sha256':fleet.sha(COMPONENT_TOOL.read_bytes()),
            'component_bundle_sha256':bundle.get('component_bundle_sha256'),'component_timer':live.get('component_timer'),
            'component_manifest_sha256':bundle.get('component_manifest_sha256'),'needs_system_components':True}

def write_plan(state_dir, plan):
    raw=canonical(plan); digest=fleet.sha(raw); directory=state_dir/'plans'; directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    path=directory/(digest+'.json')
    try:
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    except FileExistsError:
        if path.read_bytes()!=raw: raise Failure('existing plan hash collision')
    else:
        with os.fdopen(fd,'wb') as out: out.write(raw)
    return digest, path

def load_plan(state_dir, digest):
    if not re.fullmatch('[a-f0-9]{64}', digest or ''): raise Failure('invalid accepted plan hash')
    raw=(state_dir/'plans'/(digest+'.json')).read_bytes()
    if fleet.sha(raw)!=digest: raise Failure('plan hash mismatch')
    return json.loads(raw)

def matches(plan, live, device, manifest, bundle):
    gates(live, device)
    import delta_release
    delta_release.refuse_stable_overlay(live)
    if (plan['device'] != device or plan['commit'] != fleet.PIN or plan['manifest_sha256'] != bundle['manifest_sha256']
        or plan['tool_sha256'] != fleet.sha(TOOL.read_bytes()) or plan.get('component_tool_sha256') != fleet.sha(COMPONENT_TOOL.read_bytes())
        or plan.get('component_bundle_sha256') != bundle.get('component_bundle_sha256') or plan.get('component_manifest_sha256') != bundle.get('component_manifest_sha256')): raise Failure('accepted plan identity or input hash mismatch')
    if live.get('files') != plan['files'] or live.get('protected') != plan['protected'] or live.get('camd_pid') != plan['camd_pid']:
        raise Failure('preflight drift after accepted plan')
    if component_diff(live,bundle)!=plan.get('component_diff'): raise Failure('component state drift after accepted plan')
    if live.get('component_timer')!=plan.get('component_timer'): raise Failure('component timer drift after accepted plan')

def stage_and_apply(ssh, live, plan, bundle, device, command_file=None):
    if command_file is None: ssh.require_tty()
    # Baseline is explicitly the reviewed target snapshot, not a release baseline.
    live['baseline_delta']={'changed_or_missing_count':0,'entries':[]}
    live['manifest']={'commit':fleet.PIN,'sha256':bundle['manifest_sha256'],'file_count':len(plan['baseline'])}
    inputs={'candidate-code.tar.gz':Path(bundle['bundle']).read_bytes(), 'remote_transaction.py':TOOL.read_bytes(),
            'remote_components.py':COMPONENT_TOOL.read_bytes(), 'components.tar':Path(bundle['component_bundle']).read_bytes(),
            'plan.json':canonical(plan),'baseline.json':canonical(plan['baseline']), 'preflight.json':canonical(live)}
    if fleet.sha(inputs['candidate-code.tar.gz']) != bundle['bundle_sha256'] or fleet.sha(inputs['remote_transaction.py']) != plan['tool_sha256']:
        raise Failure('local bundle or transaction changed after approval')
    if (fleet.sha(inputs['components.tar'])!=plan['component_bundle_sha256'] or fleet.sha(inputs['remote_components.py'])!=plan['component_tool_sha256']):
        raise Failure('component bundle or transaction changed after approval')
    stage=ssh.run('umask 077; mktemp -d "$HOME/.headring-stage-XXXXXXXX"').decode().strip()
    if not re.fullmatch(r'/home/orangepi/\.headring-stage-[A-Za-z0-9]{8}',stage): raise Failure('unsafe remote stage path')
    for name,data in inputs.items():
        ssh.run('umask 077; cat > ' + shlex.quote(stage+'/'+name), data)
    root='/var/backups/headring-software-input/'+device+'-'+uuid.uuid4().hex
    q=shlex.quote
    commands=['set -eu','umask 077',
              'if [ ! -e /var/backups/headring-software-input ]; then mkdir -m 0700 /var/backups/headring-software-input; fi',
              'test ! -L /var/backups/headring-software-input',
              'test "$(stat -c %u:%a /var/backups/headring-software-input)" = 0:700',
              'mkdir -m 0700 '+q(root)]
    for name,data in inputs.items():
        commands += ['install -o root -g root -m 0600 '+q(stage+'/'+name)+' '+q(root+'/'+name),
                     "printf '%s\\n' "+q(fleet.sha(data)+'  '+root+'/'+name)+' | sha256sum -c -']
    commands += ['mkdir -m 0700 '+q(root+'/bundle'),'tar -xzf '+q(root+'/candidate-code.tar.gz')+' -C '+q(root+'/bundle'),
                 "printf '%s\\n' "+q(bundle['manifest_sha256']+'  '+root+'/bundle/manifest.json')+' | sha256sum -c -']
    args=['python3',root+'/remote_transaction.py','--device',device,'--bundle-dir',root+'/bundle',
          '--baseline',root+'/baseline.json','--preflight',root+'/preflight.json', '--manifest-sha256',bundle['manifest_sha256'],
          '--baseline-sha256',fleet.sha(inputs['baseline.json']),'--preflight-sha256',fleet.sha(inputs['preflight.json'])]
    component=['python3',root+'/remote_components.py','--device',device,'--archive',root+'/components.tar',
               '--plan',root+'/plan.json','--plan-sha256',fleet.sha(inputs['plan.json']),
               '--archive-sha256',plan['component_bundle_sha256'],'--manifest-sha256',plan['component_manifest_sha256']]
    dry_commands=commands+[' '.join(map(q,component)), ' '.join(map(q,args))]
    apply_commands=['set -eu',' '.join(map(q,args+['--apply'])),
                    ' '.join(map(q,component+['--apply'])), ' '.join(map(q,component+['--verify']))]
    if command_file is not None:
        command_file=Path(command_file)
        content={'device':device,'root_input':root,'preflight_observed_utc':live.get('observed_utc'),
                 'plan_sha256':fleet.sha(inputs['plan.json']),
                 'dry_run_argv':ssh.argv[:-1]+['-T',ssh.argv[-1],sudo_stdin_command('sudo sh -c '+q('; '.join(dry_commands)))],
                 'apply_verify_argv':ssh.argv[:-1]+['-T',ssh.argv[-1],sudo_stdin_command('sudo sh -c '+q('; '.join(apply_commands)))],
                 'warning':'Manual expert handoff only: run with a local PTY credential helper; do not exec as an ordinary SSH command. Run dry-run first; preflight expires after 10 minutes. No passwords are in this file.'}
        fd=os.open(command_file,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as out: json.dump(content,out,sort_keys=True,indent=2); out.write('\n')
        return {'status':'staged_not_applied','command_file':str(command_file),'root_input':root}
    commands=dry_commands+apply_commands
    try: ssh.run('sudo sh -c '+q('; '.join(commands)), interactive=True)
    except Failure as exc:
        raise Failure(str(exc)+'; code may have committed before component failure; inspect root input and receipts at '+root) from exc
    return root

def stage_components_verify(ssh,plan,bundle,device):
    ssh.require_tty()
    inputs={'remote_transaction.py':TOOL.read_bytes(),'remote_components.py':COMPONENT_TOOL.read_bytes(),
            'components.tar':Path(bundle['component_bundle']).read_bytes(),'plan.json':canonical(plan)}
    if (plan['device']!=device or fleet.sha(inputs['remote_transaction.py'])!=plan['tool_sha256']
        or fleet.sha(inputs['remote_components.py'])!=plan['component_tool_sha256']
        or fleet.sha(inputs['components.tar'])!=plan['component_bundle_sha256']
        or bundle['component_manifest_sha256']!=plan['component_manifest_sha256']):
        raise Failure('component verification inputs changed since plan')
    stage=ssh.run('umask 077; mktemp -d "$HOME/.headring-stage-XXXXXXXX"').decode().strip()
    if not re.fullmatch(r'/home/orangepi/\.headring-stage-[A-Za-z0-9]{8}',stage): raise Failure('unsafe remote stage path')
    for name,data in inputs.items(): ssh.run('umask 077; cat > '+shlex.quote(stage+'/'+name),data)
    root='/var/backups/headring-software-input/'+device+'-verify-'+uuid.uuid4().hex; q=shlex.quote
    commands=['set -eu','umask 077',
              'if [ ! -e /var/backups/headring-software-input ]; then mkdir -m 0700 /var/backups/headring-software-input; fi',
              'test ! -L /var/backups/headring-software-input',
              'test "$(stat -c %u:%a /var/backups/headring-software-input)" = 0:700',
              'mkdir -m 0700 '+q(root)]
    for name,data in inputs.items():
        commands += ['install -o root -g root -m 0600 '+q(stage+'/'+name)+' '+q(root+'/'+name),
                     "printf '%s\\n' "+q(fleet.sha(data)+'  '+root+'/'+name)+' | sha256sum -c -']
    args=['python3',root+'/remote_components.py','--device',device,'--archive',root+'/components.tar',
          '--plan',root+'/plan.json','--plan-sha256',fleet.sha(inputs['plan.json']),
          '--archive-sha256',plan['component_bundle_sha256'],'--manifest-sha256',plan['component_manifest_sha256'],'--verify']
    commands += [' '.join(map(q,args))]
    ssh.run('sudo sh -c '+q('; '.join(commands)),interactive=True)
    return root

def verify(live, plan, manifest, device, bundle=None, root_components_verified=False):
    gates(live,device)
    if live['protected'] != plan['protected']: raise Failure('protected snapshot changed')
    if live.get('camd_pid') != plan['camd_pid']: raise Failure('camd PID changed')
    for item in manifest['files']:
        if live.get('files',{}).get(item['path'],{}).get('sha256') != item['sha256']:
            raise Failure('installed code hash mismatch: '+item['path'])
    components=component_diff(live,bundle or {})
    timer=live.get('component_timer') or {}
    component_files_ok=len(components)==5 and all(x['status']=='same' or (root_components_verified and x['status']=='unknown') for x in components)
    ready=bool(root_components_verified and component_files_ok and timer=={'is-enabled':'enabled','is-active':'active'})
    return {'status':'all_ready' if ready else 'code_verified','device':device,'files':len(manifest['files']), 'deploy_ready':ready,
            'needs_system_components':not ready,'system_components':live.get('system_components',{}),'component_diff':components,'component_timer':timer,
            'usb_hardware':'not_inferred_check_component_receipt'}
