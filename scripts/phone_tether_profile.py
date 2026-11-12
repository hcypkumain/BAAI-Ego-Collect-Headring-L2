#!/usr/bin/env python3
"""Offline NM candidate generator. Never invokes nmcli or changes networking."""
import argparse
import ipaddress
import json
from pathlib import Path
import re
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'orangepi/src/orangepi_collect/common'))
from phone_transport import DRIVERS, load_config


def generate(config, id_path, driver, nm_version, subnet, routes):
    # Conservative supported floor, not a probe of the installed NM daemon.
    if not re.fullmatch(r'\d+\.\d+(?:\.\d+)?', nm_version) or tuple(map(int, nm_version.split('.')[:2])) < (1, 42):
        raise ValueError('NM >= 1.42 must be explicitly attested; older/unknown versions refused')
    if config['mode'] != 'usb_tether' or driver not in DRIVERS:
        raise ValueError('USB tether selector and approved driver required')
    if not re.fullmatch(r'[a-zA-Z0-9_.:+-]{8,256}', id_path) or '-usb-' not in id_path:
        raise ValueError('exact reviewed USB ID_PATH required, no wildcards')
    network = ipaddress.IPv4Network(subnet, strict=True)
    if not network.is_private or network.prefixlen < 16:
        raise ValueError('reviewed narrow private candidate subnet required')
    if not isinstance(routes, list):
        raise ValueError('explicit route inventory required')
    for route in routes:
        destination = route.get('dst')
        if destination == 'default':
            continue
        if not destination:
            raise ValueError('route destination missing')
        other = ipaddress.ip_network(destination, strict=False)
        if other.version == 4 and other.overlaps(network):
            raise ValueError('BLOCKED: candidate subnet overlaps existing route')
    identifier = uuid.uuid5(uuid.NAMESPACE_URL, 'headring-phone-tether:' + json.dumps(config['selector'], sort_keys=True))
    return f'''# REVIEW CANDIDATE ONLY: no activation authorization; verify actual DHCP subnet.
# VID/PID/optional serial qualification is additionally enforced by the observer.
[connection]
id=headring-phone-tether
uuid={identifier}
type=ethernet
autoconnect=false

[match]
path={id_path};
driver={driver};

[ipv4]
method=auto
never-default=true
ignore-auto-dns=true
ignore-auto-routes=true

[ipv6]
method=disabled
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--id-path', required=True)
    parser.add_argument('--driver', required=True)
    parser.add_argument('--nm-version', required=True)
    parser.add_argument('--candidate-subnet', required=True)
    parser.add_argument('--routes-json', required=True, help='offline ip -j route inventory, not collected by this tool')
    parser.add_argument('--staging', help='optional NEW candidate file, otherwise dry-run stdout')
    args = parser.parse_args()
    try:
        content = generate(load_config(args.config), args.id_path, args.driver, args.nm_version,
                           args.candidate_subnet, json.loads(Path(args.routes_json).read_text()))
        if args.staging:
            target = Path(args.staging).absolute()
            if target == Path('/etc') or Path('/etc') in target.parents or target.parent.resolve() != target.parent:
                raise ValueError('staging must be outside /etc and symlink-free')
            import os
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as stream:
                stream.write(content)
        else:
            print(content, end='')
        return 0
    except (OSError, ValueError, TypeError, AttributeError):
        print('Profile refused: invalid input, unsupported capability or route overlap', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
