import importlib.machinery,importlib.util,sys,json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
p=Path(__file__).resolve().parent.parent;sys.path.insert(0,str(p));loader=importlib.machinery.SourceFileLoader('lan',str(p/'headring-lan-time'));spec=importlib.util.spec_from_loader(loader.name,loader);m=importlib.util.module_from_spec(spec);loader.exec_module(m)
class T(unittest.TestCase):
 def test_conflict_and_boot(self):
  with tempfile.TemporaryDirectory() as tmp:
   d=Path(tmp);(d/'config').write_text('{"server":"127.0.0.1"}');(d/'boot').write_text('boot-a');(d/'marker').write_text(json.dumps({'boot_id':'boot-a','monotonic':time.monotonic()}));original=Path
   def path(value):return d/'marker' if value=='/run/headring-phone-time.json' else d/'boot' if value=='/proc/sys/kernel/random/boot_id' else original(value)
   with patch.object(m,'CONFIG',d/'config'),patch.object(m,'Path',side_effect=path),patch.object(m,'safe_to_step',return_value=(True,'ok')),patch.object(m.time,'clock_settime') as setter:
    with patch.object(m,'measure',return_value={'offset_seconds':-.001}):self.assertEqual(m.run(dry_run=True)['reason'],'phone_time_source_conflict')
    with patch.object(m,'measure',return_value={'offset_seconds':10}):self.assertEqual(m.run(dry_run=True)['reason'],'phone_time_source_conflict')
    (d/'boot').write_text('boot-b')
    with patch.object(m,'measure',return_value={'offset_seconds':-10}):self.assertEqual(m.run(dry_run=True)['status'],'dry_run')
    self.assertFalse(setter.called)
if __name__=='__main__':unittest.main()
