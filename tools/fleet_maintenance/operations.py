"""Fixed reviewed operation used by the local credential broker."""
import ipaddress, json, re, time
from pathlib import Path
import fleet, online
from host.common.device_registry import parse_registry,select_devices
from host.common.strict_json import strict_json_loads,require

REGISTRY='reviewed-headring-f5d03b7'

def registry():
    return [{'id':REGISTRY,'commit':fleet.PIN,'manifest_sha256':fleet.MANIFEST_SHA256,'files':236,'components':5,'status':'reviewed'}]

def require_config(config,registry):
    import web_config
    require(type(config) is dict,'Explicit bound configuration required')
    transport={key:config[key] for key in ('ssh_config','bind_interface','bind_address') if key in config}
    expected=web_config.setup(registry=registry,selected=tuple(config.get('devices',{})),config=transport)
    require(config==expected and config.get('registry_digest')==registry.digest,'Configuration identity/digest changed')
    return expected

def load_inputs(registry_path,targets,transport_path):
    import web_config
    registry=parse_registry(strict_json_loads(Path(registry_path).read_text()))
    select_devices(registry,targets,purpose='monitor')
    config=web_config.setup(registry=registry,selected=targets,
                            config=strict_json_loads(Path(transport_path).read_text()))
    return registry,config

def execute(state,config,device,digest,ssh_factory,progress=lambda *_:None,*,registry):
    require_config(config,registry)
    if not re.fullmatch(r'cp_\d{4}',device) or device not in config['devices'] or not re.fullmatch(r'[a-f0-9]{64}',digest): raise ValueError('invalid_scope')
    path=Path(state)/'plans'/(digest+'.json')
    if path.is_symlink() or not path.is_file() or path.stat().st_size>8388608: raise ValueError('invalid_plan')
    plan=online.load_plan(Path(state),digest)
    fleet.require_registry_plan(plan,registry=registry,device=device)
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
    try: root=fleet.stage_registry_plan(online,ssh,live,plan,bundle,device,registry=registry)
    except Exception as exc: raise PartialFailure('transaction_or_transport_failed') from exc
    progress('verifying')
    try:
        after=online.read_live(ssh,manifest)
        verified=fleet.verify_registry_plan(online,after,plan,manifest,device,bundle,registry=registry,root_components_verified=True)
        if not verified.get('deploy_ready'): raise ValueError('not_ready')
    except Exception as exc: raise PartialFailure('postverify_failed') from exc
    # Receipt root location is retained remotely by the installer; no path or raw logs returned.
    return {'status':'succeeded','code':'verified','components':'root_transaction_verified','hardware':'unknown','receipt':'remote_transaction_completed'}

class PartialFailure(RuntimeError): pass
