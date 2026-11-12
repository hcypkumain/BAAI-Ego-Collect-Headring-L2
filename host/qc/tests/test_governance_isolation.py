"""Subprocess isolation tests. Every backend is local synthetic Python, never SSH."""
import gc
import os
import subprocess
import sys
import time
import tracemalloc
import unittest
from dataclasses import asdict, replace

from host.common.strict_json import canonical_json
from host.qc.tests.governance_fixtures import (
    sample_evidence, sample_probe_policy, sample_probe_result, sample_registry,
)
from host.qc.fleet_monitor.isolated_transport import ProbeScheduler, _test_scheduler


def synthetic_factory(*, hung=(), corrupt=None, overflow=False, partial_hang=False):
    def factory(device, digest, generation, policy):
        result = sample_probe_result(device.device_id, registry_digest=digest, generation=generation,
                                     fields={'boot_id': sample_evidence('same-session-40177'),
                                             'ram_available_bytes': sample_evidence(123)})
        if corrupt:
            result = replace(result, **corrupt)
        data = canonical_json(asdict(result)) + '\n'
        script = 'import sys,time,signal\nsys.stdin.buffer.read()\n'
        if device.device_id in hung:
            script += 'signal.signal(signal.SIGTERM,signal.SIG_IGN)\ntime.sleep(60)\n'
        elif overflow:
            script += 'sys.stdout.write("X"*2000000);sys.stdout.flush();time.sleep(60)\n'
        else:
            script += 'sys.stdout.write(' + repr(data) + ');sys.stdout.flush()\n'
            if partial_hang:
                script += 'signal.signal(signal.SIGTERM,signal.SIG_IGN)\ntime.sleep(60)\n'
        return subprocess.Popen([sys.executable, '-I', '-S', '-c', script], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True, close_fds=True)
    return factory


def pump(scheduler, *, until, timeout=3):
    start = time.monotonic()
    results = []
    while time.monotonic() - start < timeout:
        scheduler.tick(time.monotonic())
        results.extend(scheduler.drain())
        if until(results):
            return results, time.monotonic() - start
        time.sleep(0.005)
    raise AssertionError(('pump deadline', scheduler.stats(), results))


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.registry = sample_registry()
        self.policy = sample_probe_policy(timeout_ms=40)
        self.schedulers = []

    def tearDown(self):
        for scheduler in self.schedulers:
            self.assertTrue(scheduler.close())

    def scheduler(self, *, devices=None, policy=None, factory=None):
        scheduler = _test_scheduler(self.registry.devices if devices is None else devices,
                                    registry_digest=self.registry.digest, policy=policy or self.policy,
                                    factory=factory or synthetic_factory())
        self.schedulers.append(scheduler)
        return scheduler

    def test_empty_zero_process_and_invalid_arguments(self):
        scheduler = self.scheduler(devices=())
        for _ in range(100):
            scheduler.tick(time.monotonic())
        self.assertEqual(scheduler.stats()['spawned'], 0)
        self.assertEqual(scheduler.drain(), ())
        for concurrency in (0, True, 65):
            with self.assertRaises(ValueError):
                ProbeScheduler((), registry_digest=self.registry.digest,
                               policy=self.policy, concurrency=concurrency)

    def test_process_wide_ledger_blocks_replacement_across_scheduler_namespaces(self):
        first = self.scheduler(devices=self.registry.devices[:1],
                               factory=synthetic_factory(hung=(self.registry.devices[0].device_id,)))
        first.tick(time.monotonic())
        second = self.scheduler(devices=self.registry.devices[:1])
        second.tick(time.monotonic())
        self.assertEqual(second.stats()['spawned'], 0)
        self.assertEqual(second.stats()['global_children'], 1)
        self.assertTrue(first.close())
        second.tick(time.monotonic())
        self.assertEqual(second.stats()['spawned'], 1)

    def test_process_wide_sixty_four_entry_budget(self):
        from host.qc.fleet_monitor import isolated_transport as transport
        # Synthetic full unreaped ledger; never pretend these are real D-state PIDs.
        scheduler = self.scheduler(devices=self.registry.devices[:1])
        self.assertEqual(len(transport._PROCESS_DOMAINS), 0)
        try:
            transport._PROCESS_DOMAINS.update({f'synthetic-{i}': None for i in range(64)})
            scheduler.tick(time.monotonic())
            self.assertEqual(scheduler.stats()['spawned'], 0)
            self.assertEqual(scheduler.stats()['global_children'], 64)
        finally:
            for i in range(64):
                del transport._PROCESS_DOMAINS[f'synthetic-{i}']

    def test_six_devices_identical_payload_per_device_generations(self):
        scheduler = self.scheduler(policy=replace(self.policy, timeout_ms=1000))
        results, _ = pump(scheduler, until=lambda rows: len({r.device_id for r in rows}) == 6)
        expected = {d.device_id for d in self.registry.devices}
        self.assertEqual({r.device_id for r in results}, expected)
        self.assertTrue(all(r.registry_digest == self.registry.digest for r in results))
        seen = {}
        for row in results:
            self.assertGreater(row.generation, seen.get(row.device_id, 0))
            seen[row.device_id] = row.generation
        self.assertLessEqual(scheduler.stats()['children'], 2)

    def test_two_real_ignoring_term_default_deadline_third_under_eight_seconds_three_rounds(self):
        devices = self.registry.devices[:3]
        hung = tuple(d.device_id for d in devices[:2])
        scheduler = self.scheduler(devices=devices, policy=replace(self.policy, timeout_ms=5000),
                                   factory=synthetic_factory(hung=hung))
        third = devices[2].device_id
        rows, elapsed = pump(scheduler,
                             until=lambda rows: sum(r.device_id == third for r in rows) >= 3,
                             timeout=8)
        self.assertLess(elapsed, 8)
        self.assertEqual(scheduler.stats()['quarantined'], 2)
        self.assertEqual(sum(r.status == 'timeout' for r in rows), 2)
        self.assertGreaterEqual(sum(r.device_id == third for r in rows), 3)
        self.assertTrue(all(r.identity_verified for r in rows if r.device_id == third))
        print('default_deadline_two_hung_three_rounds_seconds=%.3f' % elapsed)

    def test_wrong_digest_generation_device_rejected(self):
        for corrupt in ({'registry_digest': 'b'*64}, {'generation': 999}, {'device_id': 'cp_0099'}):
            scheduler = self.scheduler(devices=self.registry.devices[:1],
                                       policy=replace(self.policy, timeout_ms=1000),
                                       factory=synthetic_factory(corrupt=corrupt))
            results, _ = pump(scheduler, until=bool)
            self.assertFalse(results[-1].identity_verified)
            self.assertTrue(all(e.value_json is None for _, e in results[-1].fields))
            self.assertGreater(scheduler.stats()['rejected'], 0)

    def test_overflow_stops_and_does_not_retain_output(self):
        scheduler = self.scheduler(devices=self.registry.devices[:1],
                                   policy=replace(self.policy, maximum_bytes=4096, timeout_ms=1000),
                                   factory=synthetic_factory(overflow=True))
        results, _ = pump(scheduler, until=bool)
        self.assertFalse(results[-1].identity_verified)
        self.assertLess(scheduler.stats()['bytes'], 8192)
        self.assertEqual(scheduler.stats()['quarantined'], 1)

    def test_late_stage_after_deadline_rejected_earlier_ram_retained(self):
        def factory(device, digest, generation, policy):
            early = sample_probe_result(device.device_id, registry_digest=digest, generation=generation,
                                        fields={'ram_available_bytes': sample_evidence(123)})
            late = sample_probe_result(device.device_id, registry_digest=digest, generation=generation,
                                       fields={'ram_available_bytes': sample_evidence(999)})
            script = ('import sys,time,signal\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\n'
                      'sys.stdin.buffer.read()\n'
                      'print(' + repr(canonical_json(asdict(early))) + ',flush=True)\n'
                      'time.sleep(0.3)\n'
                      'print(' + repr(canonical_json(asdict(late))) + ',flush=True)\n'
                      'time.sleep(60)\n')
            return subprocess.Popen([sys.executable, '-I', '-S', '-c', script],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    start_new_session=True, close_fds=True)
        scheduler = self.scheduler(devices=self.registry.devices[:1],
                                   policy=replace(self.policy, timeout_ms=150), factory=factory)
        rows, _ = pump(scheduler, until=bool)
        self.assertEqual(rows[-1].status, 'timeout')
        self.assertTrue(rows[-1].identity_verified)
        self.assertEqual(dict(rows[-1].fields)['ram_available_bytes'].value_json, '123')

    def test_close_bounded_real_ignoring_term_child_and_idempotent(self):
        scheduler = self.scheduler(devices=self.registry.devices[:1],
                                   factory=synthetic_factory(hung=(self.registry.devices[0].device_id,)))
        scheduler.tick(time.monotonic())
        time.sleep(0.05)
        start = time.monotonic()
        self.assertTrue(scheduler.close(timeout_s=2))
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(scheduler.stats()['children'], 0)
        self.assertTrue(scheduler.close())
        scheduler.tick(time.monotonic())
        self.assertEqual(scheduler.stats()['spawned'], 1)

    def test_hundred_timeout_rounds_resource_budget_no_replacement(self):
        baseline_fd = len(os.listdir('/proc/self/fd'))
        scheduler = self.scheduler(devices=self.registry.devices[:2],
                                   factory=synthetic_factory(hung=tuple(d.device_id for d in self.registry.devices[:2])))
        tracemalloc.start()
        baseline_mem = tracemalloc.get_traced_memory()[0]
        pump(scheduler, until=lambda rows: len(rows) == 2, timeout=3)
        peak_fd = len(os.listdir('/proc/self/fd'))
        for _ in range(100):
            scheduler.tick(time.monotonic())
            scheduler.drain()
            peak_fd = max(peak_fd, len(os.listdir('/proc/self/fd')))
            self.assertEqual(scheduler.stats()['spawned'], 2)
            self.assertEqual(scheduler.stats()['children'], 0)
        gc.collect()
        retained, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.assertLessEqual(peak_fd, baseline_fd)
        self.assertLess(retained - baseline_mem, 1048576)
        print('100_timeout_ticks_spawned=2 children=0 fd_delta=%d retained_bytes=%d peak_bytes=%d' %
              (peak_fd-baseline_fd, retained-baseline_mem, peak))

    def test_hundred_distinct_real_timeout_processes_fd_memory_budget(self):
        def factory(*args):
            return subprocess.Popen([sys.executable, '-I', '-S', '-c', 'import time;time.sleep(60)'],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, start_new_session=True, close_fds=True)
        def rss():
            with open('/proc/self/status') as stream:
                return next(int(line.split()[1])*1024 for line in stream if line.startswith('VmRSS:'))
        base_fd, base_rss = len(os.listdir('/proc/self/fd')), rss()
        peak_fd, peak_rss, total = base_fd, base_rss, 0
        tracemalloc.start()
        for _ in range(100):
            scheduler = _test_scheduler(self.registry.devices[:1], registry_digest=self.registry.digest,
                                        policy=replace(self.policy, timeout_ms=1), factory=factory)
            scheduler.tick(time.monotonic())
            peak_fd = max(peak_fd, len(os.listdir('/proc/self/fd')))
            rows, _ = pump(scheduler, until=bool, timeout=2)
            self.assertEqual(rows[-1].status, 'timeout')
            self.assertTrue(scheduler.close())
            total += scheduler.stats()['spawned']
            self.assertEqual(scheduler.stats()['children'], 0)
            self.assertEqual(len(os.listdir('/proc/self/fd')), base_fd)
            peak_rss = max(peak_rss, rss())
        gc.collect()
        retained, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.assertEqual(total, 100)
        self.assertLessEqual(peak_fd - base_fd, 3)
        self.assertLess(retained, 1048576)
        self.assertLess(peak_rss - base_rss, 16*1048576)
        print('100_real_timeout_rounds_spawned=%d peak_fd_delta=%d rss_delta=%d retained=%d peak=%d' %
              (total, peak_fd-base_fd, peak_rss-base_rss, retained, peak))

    def test_unreapable_synthetic_ledger_frees_normal_slots_not_same_device(self):
        # Real process resources, synthetic poll() simulates kernel-unreapability.
        scheduler = self.scheduler(devices=self.registry.devices[:3],
                                   policy=replace(self.policy, timeout_ms=100),
                                   factory=synthetic_factory(hung=tuple(d.device_id for d in self.registry.devices[:2])))
        scheduler.tick(time.monotonic())
        originals = []
        for domain in scheduler._domains.values():
            process = domain.process
            original_poll = process.poll
            originals.append((process, original_poll))
            process.poll = lambda: None
        third = self.registry.devices[2].device_id
        try:
            results, elapsed = pump(scheduler,
                                    until=lambda rows: any(r.device_id == third for r in rows), timeout=3)
            self.assertLess(elapsed, 3)
            self.assertTrue(next(r for r in results if r.device_id == third).identity_verified)
            self.assertEqual(scheduler.stats()['quarantined'], 2)
            self.assertGreaterEqual(scheduler.stats()['children'], 2)
            self.assertLessEqual(scheduler.stats()['children'], 3)
            self.assertFalse(scheduler.close(timeout_s=0.03))
            before = scheduler.stats()['spawned']
            for _ in range(100):
                scheduler.tick(time.monotonic())
            self.assertEqual(scheduler.stats()['spawned'], before)
        finally:
            for process, original_poll in originals:
                process.poll = original_poll
        self.assertTrue(scheduler.close())


if __name__ == '__main__':
    unittest.main()
