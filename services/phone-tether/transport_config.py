"""Startup-only mode gate. No stopping services or automatic activation."""
import argparse
from pathlib import Path
import sys
import subprocess

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'orangepi/src'))
try:
    from orangepi_collect.common.phone_transport import load_config
except ModuleNotFoundError:
    from phone_transport import load_config


def opposite_inactive(mode):
    """Read-only startup guard. Unknown/transitioning service state fails closed."""
    unit = 'usb-phone-link.service' if mode == 'usb_tether' else 'headring-phone-tether.service'
    try:
        result = subprocess.run(['/usr/bin/systemctl', 'show', '--property=ActiveState', '--value', unit],
                                capture_output=True, text=True, timeout=3, check=False)
        return result.returncode == 0 and result.stdout.strip() in ('inactive', 'failed')
    except (OSError, subprocess.SubprocessError):
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='/etc/headring-phone-transport.json')
    parser.add_argument('--require', choices=('adb', 'usb_tether'), required=True)
    args = parser.parse_args()
    try:
        if load_config(args.config)['mode'] != args.require:
            return 1
        return 0 if opposite_inactive(args.require) else 1
    except (OSError, ValueError):
        print('Invalid transport configuration', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
