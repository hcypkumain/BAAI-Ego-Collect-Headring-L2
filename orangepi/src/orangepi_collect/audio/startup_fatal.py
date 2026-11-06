"""Best-effort alert only for a real, unrelated startup failure.

Optional audio failures never call this helper and never gate acquisition.
No CloudCache/controller/recording imports occur here.
"""
import argparse
import hashlib
import importlib
import json
from pathlib import Path
import sys
import time


def play_startup_fatal(base, timeout=12.0):
    try:
        timeout = float(timeout)
        if not 0 < timeout <= 30: return 2
        deadline = time.monotonic()+timeout
        assets = Path(base)/'assets/cloud_voice'
        with (assets/'manifest.json').open('rb') as handle: raw = handle.read(32769)
        if len(raw)>32768: return 3
        entry = json.loads(raw.decode('utf-8'))['fatal']
        # Startup preflight failures are camera/storage/controller failures.
        # The legacy clip incorrectly calls every such failure a voice outage.
        if '语音' in entry.get('text', ''):
            print('STARTUP_FAILURE: skipped misleading legacy voice clip; see service journal', file=sys.stderr)
            return 3
        expected = entry['sha256']
        wav = assets/'fatal.wav'
        digest = hashlib.sha256(); size = 0
        with wav.open('rb') as handle:
            while True:
                part = handle.read(65536)
                if not part: break
                size += len(part)
                if size>8388608 or time.monotonic()>=deadline: return 124
                digest.update(part)
        if digest.hexdigest()!=expected: return 3
        router = importlib.import_module('advisory_output').AudioRouter(base)
        result = router.play(wav,cancelled=lambda:time.monotonic()>=deadline,skipped=lambda:False)
        return 0 if result in ('played','disabled') else 124 if time.monotonic()>=deadline else 3
    except Exception:
        return 3


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--base',type=Path,default=Path(__file__).parents[3])
    parser.add_argument('--timeout',type=float,default=12.0)
    args = parser.parse_args(argv)
    return play_startup_fatal(args.base,args.timeout)


if __name__=='__main__':
    sys.exit(main())
