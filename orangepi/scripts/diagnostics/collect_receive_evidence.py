"""Read-only Linux sampler for an operator-controlled acquisition.

Writes a new diagnostic directory. Never starts/stops recording, opens devices
for streaming, changes V4L2 controls, modifies QC, or consumes tasks. No third
party dependencies. Query controls once at each end, sample procfs once/second.
This does not measure the exact duration of callback or lock waits.
"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import urllib.request


def read_text(path):
    try:
        return Path(path).read_text().strip()
    except (OSError, UnicodeError):
        return None


def cpu_ticks():
    result = {}
    for line in (read_text('/proc/stat') or '').splitlines():
        fields = line.split()
        if fields and fields[0].startswith('cpu'):
            # guest/guest_nice are already included in user/nice; don't double count.
            ticks = [int(x) for x in fields[1:9]]
            if len(ticks) >= 5:
                result[fields[0]] = ticks
    return result


def cpu_delta(previous, current):
    output = {}
    for name, ticks in current.items():
        old = previous.get(name)
        if not old or len(old) != len(ticks):
            continue
        delta = [b - a for a, b in zip(old, ticks)]
        total = sum(delta)
        if total <= 0 or min(delta) < 0:
            continue
        output[name] = {
            'busy_percent': 100. * (total - delta[3] - delta[4]) / total,
            'idle_percent': 100. * delta[3] / total,
            'iowait_percent': 100. * delta[4] / total,
            'softirq_percent': 100. * delta[6] / total if len(delta) > 6 else None,
        }
    return output


def stat_fields(text):
    # comm may contain spaces or parentheses; fields after the final ')' start at #3.
    tail = text[text.rfind(')') + 1:].split()
    if len(tail) < 37:
        raise ValueError('Short proc stat')
    return {'state': tail[0], 'cpu_ticks': int(tail[11]) + int(tail[12]),
            'start_ticks': int(tail[19]), 'cpu': int(tail[36])}


def acquisition_pids(explicit):
    if explicit:
        return explicit
    result = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdecimal():
            continue
        # Only inspect argv for matching; never copy command lines into evidence.
        try:
            words = (entry / 'cmdline').read_bytes().split(b'\0')
            if any(w.rsplit(b'/', 1)[-1] == b'capture.py' for w in words):
                result.append(int(entry.name))
        except OSError:
            pass
    return result


def thread_samples(pids, previous, elapsed, clock_hz):
    result, new = [], {}
    for pid in pids:
        task_root = Path('/proc') / str(pid) / 'task'
        try:
            tasks = list(task_root.iterdir())
        except OSError:
            continue
        for task in tasks:
            try:
                value = stat_fields((task / 'stat').read_text())
                key = (pid, int(task.name), value['start_ticks'])
                old = previous.get(key)
                ticks = value['cpu_ticks']
                value.update(pid=pid, tid=int(task.name), comm=read_text(task / 'comm'),
                             wchan=read_text(task / 'wchan'),
                             cpu_percent_of_one_core=(100. * (ticks - old) / clock_hz / elapsed
                                                      if old is not None and elapsed > 0 else None))
                status = (read_text(task / 'status') or '').splitlines()
                value['context_switches'] = {s.split(':')[0]: int(s.split(':')[1]) for s in status
                                            if s.startswith(('voluntary_ctxt_switches:', 'nonvoluntary_ctxt_switches:'))}
                result.append(value)
                new[key] = ticks
            except (OSError, ValueError):
                continue  # Thread may exit during sampling.
    return result, new


def usb_devices():
    result = []
    for path in sorted(Path('/sys/bus/usb/devices').glob('*')):
        if (path / 'idVendor').exists():
            result.append({'path': path.name, 'id': (read_text(path / 'idVendor') or '') + ':' +
                           (read_text(path / 'idProduct') or ''), 'speed_mbps': read_text(path / 'speed'),
                           'product': read_text(path / 'product')})
    return result


def camera_queries():
    nodes = {}
    for path in sorted(Path('/dev/v4l/by-path').glob('*video-index0')):
        try:
            nodes.setdefault(str(path.resolve(strict=True)), str(path))
        except OSError:
            pass
    executable = shutil.which('v4l2-ctl')
    result = []
    for node, original_path in nodes.items():
        item = {'device': node, 'by_path': original_path}
        if executable:
            try:
                # --all is a getter; there are no set/control/stream options here.
                proc = subprocess.run([executable, '-d', node, '--all'], capture_output=True,
                                      text=True, timeout=3)
                item.update(exit_code=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)
            except (OSError, subprocess.TimeoutExpired) as exc:
                item['query_error'] = type(exc).__name__
        else:
            item['query_error'] = 'v4l2-ctl unavailable'
        result.append(item)
    return result


def api_state(project):
    try:
        secret = (project / 'orangepi/runtime/dashboard_token').read_text().strip()
        request = urllib.request.Request('http://127.0.0.1:8765/api/status', headers={'X-Access-Token': secret})
        with urllib.request.urlopen(request, timeout=.4) as response:
            value = json.load(response)
        return {k: value.get(k) for k in ('state', 'busy', 'session')}
    except (OSError, ValueError):
        return {'query_available': False}


def system_values():
    memory = {}
    for line in (read_text('/proc/meminfo') or '').splitlines():
        if line.startswith(('MemAvailable:', 'SwapTotal:', 'SwapFree:', 'CmaTotal:', 'CmaFree:')):
            memory[line.split(':')[0]] = int(line.split()[1])
    temperatures = {p.parent.name: read_text(p) for p in Path('/sys/class/thermal').glob('thermal_zone*/temp')}
    frequencies = {p.name: {'current_khz': read_text(p / 'scaling_cur_freq'),
                             'maximum_khz': read_text(p / 'scaling_max_freq')}
                   for p in Path('/sys/devices/system/cpu/cpufreq').glob('policy*')}
    return {'loadavg': read_text('/proc/loadavg'), 'memory_kib': memory,
            'temperatures_mC': temperatures, 'cpu_frequency': frequencies}


def write_json(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path('/home/orangepi/Desktop/Embodied-Brain-Collect-main'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seconds', type=int, default=240)
    parser.add_argument('--pid', type=int, action='append', default=[])
    args = parser.parse_args()
    if not 1 <= args.seconds <= 900:
        parser.error('--seconds must be 1..900')
    # Existing directories are rejected to preserve previous evidence.
    args.output.mkdir(parents=True, exist_ok=False)
    start = time.time()
    boot = read_text('/proc/sys/kernel/random/boot_id')
    write_json(args.output / 'start.json', {'time': start, 'boot_id': boot,
               'usb': usb_devices(), 'camera_queries': camera_queries(),
               'limits': 'One-second system samples; no exact callback duration, queue depth, or lock wait duration.'})
    ticks_before, threads_before = cpu_ticks(), {}
    clock_hz = os.sysconf('SC_CLK_TCK')
    last = time.monotonic()
    deadline = last + args.seconds
    samples = 0
    try:
        with (args.output / 'samples.jsonl').open('x', encoding='utf-8', buffering=1) as stream:
            while time.monotonic() < deadline:
                time.sleep(min(1., max(0., deadline - time.monotonic())))
                now = time.monotonic()
                ticks_after = cpu_ticks()
                threads, threads_before = thread_samples(acquisition_pids(args.pid), threads_before,
                                                         now - last, clock_hz)
                row = {'time': time.time(), 'monotonic': now, 'sample_elapsed_seconds': now - last,
                       'boot_id': read_text('/proc/sys/kernel/random/boot_id'),
                       'cpu': cpu_delta(ticks_before, ticks_after), 'threads': threads,
                       'api': api_state(args.project), 'usb': usb_devices()}
                row.update(system_values())
                stream.write(json.dumps(row, ensure_ascii=False) + '\n')
                samples += 1
                ticks_before, last = ticks_after, now
                if row['boot_id'] != boot:
                    break
    except KeyboardInterrupt:
        pass
    finally:
        write_json(args.output / 'end.json', {'time': time.time(), 'samples': samples,
                   'boot_id': read_text('/proc/sys/kernel/random/boot_id'), 'usb': usb_devices(),
                   'camera_queries': camera_queries()})
        journal = shutil.which('journalctl')
        if journal:
            try:
                proc = subprocess.run([journal, '-k', '-b', '--since', '@' + str(int(start)),
                                       '--no-pager', '-o', 'short-iso'], capture_output=True, text=True, timeout=5)
                (args.output / 'kernel.log').write_text(proc.stdout + proc.stderr, encoding='utf-8')
            except (OSError, subprocess.TimeoutExpired):
                pass
    print(json.dumps({'output': str(args.output), 'samples': samples}))


if __name__ == '__main__':
    main()
