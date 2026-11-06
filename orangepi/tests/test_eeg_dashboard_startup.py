"""Instantiate the EEG dashboard with the current controller interface, no sockets."""
import pathlib,sys,tempfile,unittest
from unittest.mock import Mock,patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'))
import project_paths
import dashboard

class EegDashboardStartup(unittest.TestCase):
 def test_current_controller_constructor_retains_eeg_ui_and_importer(self):
  with tempfile.TemporaryDirectory() as temp:
   root=pathlib.Path(temp);(root/'runtime').mkdir();importer=object()
   with patch.object(dashboard,'DashboardHTTPServer',return_value=Mock()) as server,patch('phone_links.PhoneLinks'),patch('device_diagnostics.DeviceDiagnostics'),patch('camera_check.CameraChecks'):
    owner=dashboard.Dashboard(root,root/'preview',{'host':'127.0.0.1','port':0,'access_code_required':False},usb_task_importer=importer)
   self.assertIs(owner.usb_task_importer,importer)
   self.assertIn('eeg',dashboard.SIGNALS)
   self.assertIn('eeg_filter',dashboard.actions_for('device_confirm'))
   self.assertIn(b'id="eeg"',owner.page)
   handler_class=server.call_args.args[1]
   handler=handler_class.__new__(handler_class)
   handler.path='/api/status?signals=eeg&review=0&display=fixture&cameras=0'
   handler.client_address=('127.0.0.1',1);handler.headers={};handler.authorized=lambda:True;handler.send=Mock()
   with patch.object(dashboard,'handle_phone_request',return_value=False),patch.object(dashboard,'handle_reference_request',return_value=False):handler.do_GET()
   self.assertEqual(handler.send.call_args.args[0],200)

if __name__=='__main__':unittest.main()
