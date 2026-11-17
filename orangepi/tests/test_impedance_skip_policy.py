import ast
from dataclasses import dataclass
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1] / 'src/orangepi_collect'


def load_class(path, name, namespace):
    tree = ast.parse(path.read_text())
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)
    return namespace[name]


class SkipPolicyTests(unittest.TestCase):
    def test_skip_is_limited_to_four_devices_and_recording_workers(self):
        tree=ast.parse((ROOT/'acquisition/eeg_worker.py').read_text())
        expr=next(n.value for n in ast.walk(tree) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='skip_impedance' for t in n.targets))
        code=compile(ast.Expression(expr),'<skip-policy>','eval')
        for device in ('cp_0013','cp_0017','cp_0018','cp_0023','cp_0022'):
            for output in ('capture',None):
                path=lambda _p, d=device:types.SimpleNamespace(read_text=lambda:d)
                result=eval(code,{'Path':path,'args':types.SimpleNamespace(output=output)})
                self.assertEqual(result,bool(output) and device!='cp_0022')

    def test_fresh_readiness_is_required_after_eeg_disconnect_and_reconnection(self):
        tree = ast.parse((ROOT/'acquisition/capture.py').read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and any(isinstance(m, ast.FunctionDef) and m.name == 'required_readiness' for m in n.body))
        fn = next(m for m in cls.body if isinstance(m, ast.FunctionDef) and m.name == 'required_readiness')
        env = {'time': types.SimpleNamespace(monotonic=lambda: 100.0), 'MAX_READY_AGE_SECONDS': 3}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), '<readiness>', 'exec'), env)
        eeg = types.SimpleNamespace(name='eeg', ready=True, last=100.0, seen=10)
        capture = types.SimpleNamespace(required_contract={'required_devices':['eeg']}, cameras=[], emgs=[], board=None, eeg=eeg)
        self.assertTrue(env['required_readiness'](capture)['ready'])
        eeg.last = 96.0
        self.assertEqual(env['required_readiness'](capture)['missing'], ['eeg'])
        eeg.ready = False; eeg.last = 100.0
        self.assertFalse(env['required_readiness'](capture)['ready'])
        eeg.ready = True; eeg.last = 100.0; eeg.seen = 11
        self.assertTrue(env['required_readiness'](capture)['ready'])

    def test_failed_worker_forces_a_new_scan_after_owner_exits(self):
        from unittest.mock import Mock
        tree=ast.parse((ROOT/'control/launcher.py').read_text())
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and any(isinstance(m,ast.FunctionDef) and m.name=='poll' for m in n.body))
        poll=next(m for m in cls.body if isinstance(m,ast.FunctionDef) and m.name=='poll')
        prepare=next(m for m in cls.body if isinstance(m,ast.FunctionDef) and m.name=='prepare')
        env={'time':types.SimpleNamespace(monotonic=lambda:100.0), 'BASE':Path('/nonexistent')}
        exec(compile(ast.Module(body=[poll,prepare],type_ignores=[]),'<controller>','exec'),env)
        c=types.SimpleNamespace(worker=types.SimpleNamespace(poll=lambda:None),state='recording',voice=types.SimpleNamespace(),notified_error=False,
            scan_ok=True,scan_retry_pending=False,worker_status=lambda:{'state':'error','errors':['eeg: no data for 3 seconds']},
            _assert_controller_thread=Mock(),poll_stop=Mock(),state_write=Mock(),error_voice=Mock())
        env['poll'](c);self.assertFalse(c.scan_ok);self.assertTrue(c.scan_retry_pending)
        c.worker=None;c.retry_pending_owner=Mock(return_value=True);c.stop_preview=Mock(return_value=True)
        c._reference_mutation_started=Mock();c.scan_devices=Mock(return_value=False);c.last_device_scan=99.0
        self.assertFalse(env['prepare'](c));c.scan_devices.assert_called_once_with(announce=False,quiet=True)

    def test_skipped_qc_requires_matching_identity_and_fresh_real_data(self):
        fake = types.ModuleType('impedance_record')
        def invalid(*args, **kwargs): raise ValueError('missing impedance proof')
        fake.validate_start_binding = invalid
        old = sys.modules.get('impedance_record'); sys.modules['impedance_record'] = fake
        class Output:
            def __init__(self): self.findings=[]; self.stats={}
        class Base:
            def finding(self, level, message, **kw): return (level, message)
        try:
            check = load_class(ROOT/'framework/eeg_qc.py', 'OmniEegImpedance', dict(dataclass=dataclass, BaseCheck=Base, CheckOutput=Output, Path=Path, json=json))()
            for device, prefix in [('cp_0013','L2-001-'),('cp_0017','L2-017-'),('cp_0018','L2-018-'),('cp_0023','L2-023-')]:
                with self.subTest(device=device), tempfile.TemporaryDirectory() as temp:
                    root=Path(temp)/(prefix+'2026-11-17-20-00-00'); eeg=root/'eeg'; eeg.mkdir(parents=True)
                    (eeg/'metadata.json').write_text('{}')
                    gate={'eeg_impedance':{'version':1,'device_id':device,'status':'not_measured','reason':'operator_requested_skip'},'required_devices':['eeg'],'required_device_readiness':{'eeg':{'ready':True,'fresh':True,'seen':10}}}
                    def run():
                        (root/'start_gate.json').write_text(json.dumps(gate));return check.run(types.SimpleNamespace(dir=eeg)).findings[0][0]
                    self.assertEqual(run(), 'WARN')
                    gate['required_device_readiness']['eeg']['fresh']=False;self.assertEqual(run(), 'ERROR')
                    gate['required_device_readiness']['eeg'].update(fresh=True,seen=0);self.assertEqual(run(), 'ERROR')
                    gate['required_device_readiness']['eeg']['seen']=10;gate['eeg_impedance']['device_id']='cp_9999';self.assertEqual(run(), 'ERROR')
        finally:
            if old is None: sys.modules.pop('impedance_record',None)
            else: sys.modules['impedance_record']=old

if __name__ == '__main__': unittest.main()
