import ast,json,sys,tempfile,types,unittest
from dataclasses import dataclass
from pathlib import Path
p=Path(__file__).resolve().parents[1]/'src/orangepi_collect/framework/eeg_qc.py';tree=ast.parse(p.read_text());cls=next(x for x in tree.body if isinstance(x,ast.ClassDef) and x.name=='OmniEegImpedance');m=types.ModuleType('impedance_record');m.validate_start_binding=lambda *a,**kw:(_ for _ in ()).throw(ValueError('measured proof missing'));sys.modules['impedance_record']=m
class O:
 def __init__(self):self.findings=[];self.stats={}
class B:
 def finding(self,level,message,**kw):return {'level':level,'message':message,**kw}
scope={'dataclass':dataclass,'BaseCheck':B,'CheckOutput':O,'Path':Path,'json':json};exec(compile(ast.fix_missing_locations(ast.Module(body=[cls],type_ignores=[])),str(p),'exec'),scope)
class Test(unittest.TestCase):
 def evaluate(self,id,storage,ready=True,seen=10,marker=True,version=1,mismatch=False,metadata_device=None,session_id='recording-1',missing_fields=False):
  with tempfile.TemporaryDirectory() as d:
   r=Path(d);e=r/'eeg';e.mkdir();(e/'metadata.json').write_text(json.dumps({'headring_device_id':metadata_device or id}));(r/'meta.json').write_text(json.dumps({'session_id':'recording-1','storage_config':{'device_id':storage}}));gate={'required_devices':['eeg'],'required_device_readiness':{'eeg':{'ready':ready,'fresh':True,'seen':seen}},'eeg_impedance':{'version':1,'device_id':id,'status':'not_measured','reason':'operator_requested_skip'} if marker else None};
   if marker and version==2:gate['eeg_impedance'].update(version=2,session_id='wrong' if mismatch else session_id,storage_device_id=storage)
   if marker and version==2 and missing_fields:gate['eeg_impedance'].pop('storage_device_id')
   (r/'start_gate.json').write_text(json.dumps(gate));return scope['OmniEegImpedance']().run(types.SimpleNamespace(dir=e))
 def test_four_existing_device_markers(self):
  for id,storage in [('cp_0013','orangepi-01'),('cp_0017','orangepi-cp0017'),('cp_0018','orangepi-cp0018'),('cp_0023','orangepi-cp0023')]:
   out=self.evaluate(id,storage);self.assertFalse(out.stats['impedance_measured']);self.assertEqual(out.findings[0]['level'],'WARN')
 def test_v2_binds_frozen_session_and_storage(self):
  self.assertFalse(self.evaluate('cp_0021','arbitrary-provisioned-label',version=2).stats['impedance_measured'])
  self.assertEqual(self.evaluate('cp_0021','arbitrary-provisioned-label',version=2,mismatch=True).findings[0]['level'],'ERROR')
  for out in [self.evaluate('cp_0021','arbitrary',version=2,metadata_device='cp_0017'),self.evaluate('cp_0021','arbitrary',version=2,session_id=''),self.evaluate('cp_0021','arbitrary',version=2,missing_fields=True)]:self.assertEqual(out.findings[0]['level'],'ERROR')
 def test_unmatched_device_or_no_eeg_data_fails(self):
  for out in [self.evaluate('cp_0018','orangepi-cp0017'),self.evaluate('cp_0017','orangepi-cp0017',ready=False),self.evaluate('cp_0017','orangepi-cp0017',seen=0),self.evaluate('bad','orangepi-cp0017'),self.evaluate('cp_0017','orangepi-cp0017',marker=False)]:self.assertEqual(out.findings[0]['level'],'ERROR')
unittest.main()
