import json,sys,types,unittest
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[3]/'orangepi/src/orangepi_collect/control'));import phone_http as m
class H:
 headers={'Origin':'http://device','Host':'device'}
 def authorized(self):return True
class O: snapshot={'collection_context':'eeg:1'}
class Test(unittest.TestCase):
 def test_authorization_and_nonce(self):
  a=m.PhoneTime(O());h=H();response=types.SimpleNamespace(returncode=0,stdout='{"ok":true}')
  with patch.object(m.Path,'read_text',return_value='cp_0023'),patch.object(m.subprocess,'run',return_value=response) as call:
   status,ch=a.issue(h);self.assertEqual(status,200);payload=dict(ch,epoch_ms=1794900000000);self.assertEqual(a.execute(h,payload)[0],200);self.assertEqual(a.execute(h,payload)[0],409);self.assertEqual(call.call_count,1);self.assertEqual(call.call_args.kwargs['timeout'],26)
   status,ch=a.issue(h);self.assertEqual(a.execute(h,dict(ch,epoch_ms=True))[0],400)
   status,ch=a.issue(h);a.owner.snapshot={'collection_context':'emg:2'};self.assertEqual(a.execute(h,dict(ch,epoch_ms=1794900000000))[0],409)
   h.headers=dict(h.headers,Origin='http://elsewhere');self.assertEqual(a.issue(h)[0],403)
   h.headers=dict(h.headers,Origin='http://device');status,ch=a.issue(h);a.pending=(*a.pending[:3],0);self.assertEqual(a.execute(h,dict(ch,epoch_ms=1794900000000))[0],409)
 def test_device_identity_is_read_locally(self):
  for ident in ('cp_0017','cp_0022','cp_0023'):
   api=m.PhoneTime(O())
   with patch.object(m.Path,'read_text',return_value=ident):
    status,result=api.issue(H());self.assertEqual(status,200);self.assertEqual(result['device_id'],ident)
  with patch.object(m.Path,'read_text',return_value='not-a-device'):
   self.assertEqual(m.PhoneTime(O()).issue(H())[0],409)
if __name__=='__main__':unittest.main()
