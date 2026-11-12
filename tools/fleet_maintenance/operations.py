"""Fixed reviewed operation used by the local credential broker."""
import ipaddress, json, re, time
from pathlib import Path
import fleet, online

REGISTRY='reviewed-headring-f5d03b7'

def registry():
    return [{'id':REGISTRY,'commit':fleet.PIN,'manifest_sha256':fleet.MANIFEST_SHA256,'files':236,'components':5,'status':'reviewed'}]

def execute(state,config,device,digest,ssh_factory,progress=lambda *_:None):
    if not re.fullmatch(r'cp_\d{4}',device) or device not in config['devices'] or not re.fullmatch(r'[a-f0-9]{64}',digest): raise ValueError('invalid_scope')
    path=Path(state)/'plans'/(digest+'.json')
    if path.is_symlink() or not path.is_file() or path.stat().st_size>8388608: raise ValueError('invalid_plan')
    plan=online.load_plan(Path(state),digest)
    if plan.get('device')!=device or plan.get('commit')!=fleet.PIN or plan.get('unknown_approved'): raise ValueError('plan_not_approved')
    address=str(ipaddress.IPv4Address(config['devices'][device]))
    progress('preflight')
    bundle=fleet.build(Path(state)/'broker-bundles')
    raw=(fleet.RESOURCES/'manifest.json').read_bytes()
    if fleet.sha(raw)!=fleet.MANIFEST_SHA256: raise ValueError('manifest_invalid')
    manifest=json.loads(raw)
    ssh=ssh_factory(config['ssh_config'],device,address,config.get('bind_interface'),config.get('bind_address'))
    live=online.read_live(ssh,manifest)
    online.matches(plan,live,device,manifest,bundle)
    same=all(live.get('files',{}).get(x['path'],{}).get('sha256')==x['sha256'] for x in manifest['files'])
    comps=online.component_diff(live,bundle)
    timer=live.get('component_timer') or {}
    if same and len(comps)==5 and all(x['status']=='same' for x in comps) and timer=={'is-enabled':'enabled','is-active':'active'}:
        return {'status':'skipped_current','code':'matched','components':'matched_unprivileged','hardware':'unknown'}
    progress('applying')
    try: root=online.stage_and_apply(ssh,live,plan,bundle,device)
    except Exception as exc: raise PartialFailure('transaction_or_transport_failed') from exc
    progress('verifying')
    try:
        after=online.read_live(ssh,manifest)
        verified=online.verify(after,plan,manifest,device,bundle,root_components_verified=True)
        if not verified.get('deploy_ready'): raise ValueError('not_ready')
    except Exception as exc: raise PartialFailure('postverify_failed') from exc
    # Receipt root location is retained remotely by the installer; no path or raw logs returned.
    return {'status':'succeeded','code':'verified','components':'root_transaction_verified','hardware':'unknown','receipt':'remote_transaction_completed'}

class PartialFailure(RuntimeError): pass
