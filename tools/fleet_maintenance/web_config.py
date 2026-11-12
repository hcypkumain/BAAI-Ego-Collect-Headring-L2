"""Pure UI adapter. The executable owns explicit file loading."""
from pathlib import Path
from host.common.device_registry import Registry, select_devices

STATE = Path.home()/'.local/state/headring-fleet-maintenance'

def setup(*, registry: Registry, selected, config):
    devices = select_devices(registry, selected, purpose='monitor')
    allowed = {'ssh_config', 'bind_interface', 'bind_address'}
    if set(config) - allowed or not config.get('ssh_config'):
        raise ValueError('explicit transport config required; no inventory/cache fields')
    return {**config, 'devices': {d.device_id: d.address for d in devices},
            'registry_digest': registry.digest,
            'identities': {d.device_id: {'ssh_alias': d.ssh_alias,
                                       'ssh_user': d.ssh_user,
                                       'host_key_alias': d.host_key_alias} for d in devices}}
