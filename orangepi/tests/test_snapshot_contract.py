import copy
import ast
from types import SimpleNamespace
import importlib.util
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'orangepi/src/orangepi_collect/control'))
from snapshot_contract import loads, normalize, validate, aged
from snapshot_publisher import SnapshotPublisher


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.value = loads((ROOT/'contracts/v1/fixtures/device.json').read_bytes())

    def test_all_original_cases(self):
        spec = importlib.util.spec_from_file_location('examples', ROOT/'contracts/v1/validate_examples.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        module.main()

    def test_strict_json_and_semantics(self):
        for raw in ('{"seq":1,"seq":2}', '{"x":NaN}', '{"x":Infinity}'):
            with self.assertRaises(ValueError): loads(raw)
        for change in (
            lambda v: v.update(token='synthetic-negative'),
            lambda v: v['resources'].update(ram_mem_available_bytes=-1),
            lambda v: v['progress'].update(remaining=4),
            lambda v: v['sample'].update(boot_id='66666666-6666-4666-8666-666666666666'),
            lambda v: v['sample'].update(sampled_mono_ms=100001),
            lambda v: v.update(seq=True),
            lambda v: v.update(seq=-1),
        ):
            value=copy.deepcopy(self.value);change(value)
            with self.assertRaises(Exception): validate(value)

    def test_unknown_is_not_zero_and_ledger_independent(self):
        value=normalize('cp_0015')
        self.assertIsNone(value['resources']['ram_mem_available_bytes'])
        self.assertIsNone(value['health']['impedance']['can_review'])
        value['progress']['completed']=4
        validate(value)  # An independent accepted count is not thrown away.
        value=copy.deepcopy(self.value);value['session']['task_group_id']=None
        validate(value)  # Missing task-block grouping does not invalidate ledger.
        with self.assertRaises(ValueError):normalize('cp_0014',self.value)
        with self.assertRaises(ValueError):normalize('cp_0015',self.value,boot_id='66666666-6666-4666-8666-666666666666')

    def test_actual_launcher_lifecycle_hook(self):
        source=ast.parse((ROOT/'orangepi/src/orangepi_collect/control/launcher.py').read_text())
        controller=next(n for n in source.body if isinstance(n,ast.ClassDef) and n.name=='Controller')
        method=next(n for n in controller.body if isinstance(n,ast.FunctionDef) and n.name=='start_dashboard')
        class Base:
            def start_dashboard(self):self.dashboard=SimpleNamespace(boot_id='11111111-1111-4111-8111-111111111111')
        context={'Base':Base}
        cls=ast.ClassDef(name='Lifecycle',bases=[ast.Name(id='Base',ctx=ast.Load())],keywords=[],body=[method],decorator_list=[])
        tree=ast.fix_missing_locations(ast.Module(body=[cls],type_ignores=[]))
        exec(compile(tree,'launcher.py','exec'),context)
        obj=context['Lifecycle']();obj.snapshot_publisher=SnapshotPublisher('cp_0015',self.value['boot_id'],clock=lambda:100000)
        obj.snapshot_publisher.publish();old=obj.snapshot_publisher.controller_instance
        obj.start_dashboard()
        self.assertIsNone(obj.dashboard.snapshot_publisher.read())
        self.assertNotEqual(old,obj.snapshot_publisher.controller_instance)

    def test_publish_immutable_reset_and_completed_review(self):
        clock=[100000]
        p=SnapshotPublisher(self.value['device_id'],self.value['boot_id'],self.value['controller_instance'],clock=lambda:clock[0])
        item=p.publish(self.value)
        self.value['resources']['ram_mem_available_bytes']=0
        for _ in range(100):self.assertIs(p.read(),item)
        self.assertEqual(p.seq,1)
        self.assertNotEqual(loads(item.body)['resources']['ram_mem_available_bytes'],0)
        clock[0]+=60000
        self.assertEqual(p.observation()['sample']['quality'],'stale')
        self.assertEqual(p.seq,1)
        value=loads(item.body);i=value['health']['impedance']
        i.update(measurement_id='m1',owner_run='o1',session_id=value['session']['session_id'],profile_epoch=3,quality_version='q1',quality='pass',can_review=True)
        validate(aged(value,60000))  # Completed authority does not expire with observation.
        p.reset(p.device_id,p.boot_id)
        self.assertIsNone(p.read())
        with self.assertRaises(ValueError):p.publish(value,origin_instance=value['controller_instance'])
        item=p.publish()
        self.assertIsNone(loads(item.body)['health']['impedance']['can_review'])
        self.assertIsNone(loads(item.body)['resources']['disk_available_bytes'])


if __name__=='__main__':unittest.main()
