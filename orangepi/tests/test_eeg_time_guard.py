"""Idle clock correction must not interrupt an EEG impedance measurement."""
import importlib.util,io,json,pathlib,tempfile,unittest
from unittest.mock import patch
ROOT=pathlib.Path(__file__).resolve().parents[2]

class EegTimeGuard(unittest.TestCase):
 def test_impedance_active_or_pending_defers_clock_step(self):
  file=ROOT/'deployment/lan-time/headring_lan_time.py'
  spec=importlib.util.spec_from_file_location('eeg_time_fixture',file);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
  with tempfile.TemporaryDirectory() as temp:
   root=pathlib.Path(temp);(root/'runtime').mkdir();(root/'runtime/dashboard_token').write_text('test-token-for-local-fixture-only')
   session=root/'prepared';session.mkdir();module.BASE=root
   display={'state':'ready','session':str(session),'worker':{'state':'ready','recording_active':False},'privacy':{'required':False}}
   for active,pending in [(True,False),(False,True),(False,False)]:
    with self.subTest(active=active,pending=pending):
     (session/'status.json').write_text(json.dumps({'eeg_impedance':{'active':active,'pending':pending}}))
     with patch.object(module.urllib.request,'urlopen',side_effect=lambda *a,**k:io.StringIO(json.dumps(display))):allowed,reason=module.safe_to_step()
     self.assertFalse(allowed)
     if active or pending:self.assertEqual(reason,'eeg_impedance_measurement_active')
     else:self.assertEqual(reason,'application_ui_active')

if __name__=='__main__':unittest.main()
