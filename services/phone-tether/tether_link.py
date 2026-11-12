"""Read-only USB observer. Public status excludes selector, serial and credentials."""
import argparse
import json
import os
import sys
import time
import uuid
from transport_config import load_config
try:
    from orangepi_collect.common.phone_transport import CACHE, ip_rows, observe, read_cache, write_cache
except ModuleNotFoundError:
    from phone_transport import CACHE, ip_rows, observe, read_cache, write_cache


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('once', 'status', 'monitor'), nargs='?', default='status')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--config', default='/etc/headring-phone-transport.json')
    parser.add_argument('--cache', default=CACHE)
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        if args.command == 'status' and not args.once:
            print(json.dumps(read_cache(args.cache, config)))
            return 0
        if os.getuid() == 0:
            raise ValueError('observer writer must not run as root')
        instance = str(uuid.uuid4())
        while True:
            try:
                result = observe(config, ip_rows())
            except Exception:
                result = {'state': 'error', 'reason': 'observation_failed', 'links': []}
            write_cache(args.cache, config, result, instance)
            if args.command != 'monitor' or args.once:
                print(json.dumps(result))
                return 0
            time.sleep(1)
    except (OSError, ValueError):
        print('Transport configuration/cache error', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
