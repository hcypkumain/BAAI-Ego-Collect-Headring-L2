"""EEG-specific controls must keep the common report, voice and task contracts."""
import ast,importlib.util,json,pathlib,sys,unittest
from unittest.mock import Mock
ROOT=pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'orangepi/src/orangepi_collect/common'))
import project_paths,dashboard,web_controls

def load(name,path):
 spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m

class EegSharedInterfaces(unittest.TestCase):
 def test_both_report_adapters_keep_brain_labels_and_emg_advice(self):
  control=load('qc_details_control_fixture',ROOT/'orangepi/src/orangepi_collect/control/qc_details.py')
  media=load('qc_details_media_fixture',ROOT/'services/phone-media/qc_details.py')
  report={'level':'ERROR','scope':'emg_only','window':{'t0':100.},'streams':{
   'eeg':{'findings':[{'level':'WARN','check':'EEGFixture','message':'脑电告警','subject':'eeg'}]},
   'emg_right':{'findings':[{'level':'ERROR','check':'TimestampGap','message':'缺口','subject':'emg','observed':.12,'threshold':.1}]}}}
  a=control.summarize_qc(report);b=media.summarize_qc(report)
  self.assertEqual(a,b);self.assertEqual(a['scope'],'emg_only')
  right=next(x for x in a['findings'] if x['modality']=='emg_right')
  self.assertIn('120.0',right['reason']);self.assertTrue(right['action'])
  self.assertEqual(next(x for x in a['findings'] if x['modality']=='eeg')['label'],'脑电')

 def test_quality_voice_identifies_the_side_without_device_crash_advice(self):
  tree=ast.parse((ROOT/'orangepi/src/orangepi_collect/control/launcher.py').read_text())
  cls=next(x for x in tree.body if isinstance(x,ast.ClassDef) and any(isinstance(n,ast.FunctionDef) and n.name=='error_voice' for n in x.body))
  method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='error_voice');scope={}
  exec(compile(ast.Module(body=[method],type_ignores=[]),'voice_fixture','exec'),scope)
  owner=Mock();owner.last_help=[]
  scope['error_voice'](owner,['右臂肌电即时质检：时间线缺口120毫秒。处理建议：保留原始数据。'])
  self.assertIn('右臂肌电即时质检',owner.voice.say.call_args.args[0]);owner.troubleshooting.assert_not_called()

 def test_reference_dispatch_uses_ready_selection_gate(self):
  c=web_controls.WebControls();c.state='ready';c.ui_revision=1;c.voice=Mock();c.keyboard=None;c.worker=None;c.selected_devices=['eeg'];c.OwnerExited=type('OwnerExited',(RuntimeError,),{})
  c.dashboard=Mock();c.dashboard.take.return_value={'action':'reference_list','revision':1,'value':{'list_id':'task_b','catalog_revision':1}}
  c.worker_status=lambda:{'eeg_impedance':{'required':False}};c.reference_can_change=lambda:False;c.reference_can_switch_list=lambda:True
  c.select_reference_list=Mock();c.ui_publish=Mock();c.ensure_preview=Mock();c.state_write=Mock()
  c.dashboard_tick();c.select_reference_list.assert_called_once_with({'list_id':'task_b','catalog_revision':1})

 def test_idle_common_controls_and_impedance_lock_coexist(self):
  values=dashboard.actions_for('device_confirm')
  self.assertTrue({'eeg_filter','qc_mode','usb_task_import'}<=set(values))
  busy=dashboard.snapshot_actions({'state':'ready','eeg_impedance':{'required':True,'supported':True,'active':True},'task_list':{'manual_task_switch':True,'can_switch':False},'privacy':{'required':False}})
  self.assertNotIn('start',busy);self.assertNotIn('reference_list',busy);self.assertIn('impedance_end',busy)

if __name__=='__main__':unittest.main()
