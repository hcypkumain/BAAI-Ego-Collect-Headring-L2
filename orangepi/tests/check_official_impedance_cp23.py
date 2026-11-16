import sys, types, unittest, tempfile, time
from dataclasses import dataclass
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'))
sdk=types.ModuleType('omnibci');sdk.SDK_REVISION='843013ba62f1f05098c8639a657585720ce66a72';sys.modules['omnibci']=sdk
from official_impedance import OfficialPreflight
from impedance_record import validate_preflight, bind_start, validate_start_binding
from impedance_quality import quality_allows_recording
@dataclass
class Config:
 reference:str='srb1';mode:str='eeg';enabled_mask:int=255;bias_mask:int=255;srb2_mask:int=255;gains:tuple=(24,)*8;verified:bool=True
class Arr:
 def __init__(self,shape,values):self.shape=shape;self.values=values
 def __getitem__(self,x):return self.values[x[1]] if isinstance(x,tuple) else self.values[x]
class Batch:
 sample_rate_hz=250;generation=3
 def __init__(self):
  self.received_at=time.monotonic();self.eeg_uv=Arr((1,8),[1.0]*8);self.valid=Arr((1,),[True]);self.mode=Arr((1,),[1])
 def __len__(self):return 1
class Board:
 def __init__(self,values=None):self.cfg=Config();self.values=values or (10.0,)*8;self.started=False
 def get_config(self):return self.cfg
 def measure_impedance(self,**kw):return types.SimpleNamespace(mask=255,series_kohm=9.98,values_kohm=self.values,clipped_mask=2 if self.values[1] is None else 0)
 def start(self,timeout):self.started=True
 def read(self,n,timeout):return Batch()
class Tests(unittest.TestCase):
 def preflight(self,board,path):return OfficialPreflight(board,{'labels':['FC3','C3','O1','Pz','Oz','O2','C4','FC4'],'port':'/dev/eeg','serial':'','impedance_acceptance_policy':'advisory'},path,lambda *a,**kw:None,True)
 def test_success_review_and_persisted_start_validation(self):
  with tempfile.TemporaryDirectory() as d:
   b=Board();p=self.preflight(b,d);p.handle({'command':'IMPEDANCE_START','request_id':'r'});self.assertTrue(b.started);self.assertTrue(quality_allows_recording(p.state));p.handle({'command':'IMPEDANCE_REVIEW'});self.assertTrue(p.state['can_record']);validate_preflight(d,expected_state=p.state); gate={'start_monotonic_ns':time.monotonic_ns(),'start_unix_ns':time.time_ns()}; proof=bind_start(d,gate,expected_state=p.state);gate['eeg_impedance']=proof;validate_start_binding(d,gate)
 def test_invalid_channel_cannot_continue(self):
  with tempfile.TemporaryDirectory() as d:
   p=self.preflight(Board((10,None,10,10,10,10,10,10)),d);p.handle({'command':'IMPEDANCE_START','request_id':'r'});self.assertEqual(p.state['channels'][1]['reason'],'adc_saturated');self.assertFalse(quality_allows_recording(p.state));self.assertRaises(RuntimeError,p.handle,{'command':'IMPEDANCE_REVIEW'})
 def test_unverified_recovery_fails_closed(self):
  with tempfile.TemporaryDirectory() as d:
   b=Board();p=self.preflight(b,d)
   def measure(**kw): b.cfg.verified=False;return types.SimpleNamespace(mask=255,series_kohm=9.98,values_kohm=(10.,)*8,clipped_mask=0)
   b.measure_impedance=measure;self.assertRaises(RuntimeError,p.handle,{'command':'IMPEDANCE_START'});self.assertEqual(p.state['phase'],'fault');self.assertFalse(p.state['can_record'])
if __name__=='__main__':unittest.main()
