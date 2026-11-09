"""Bounded scan results cannot become ready under the wrong identity."""
import json,pathlib,sys,unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'))
from scan_result import encode,decode,MAX_BYTES

class ScanResultTests(unittest.TestCase):
 def setUp(self):
  self.fp=[['wrist','/dev/path','/dev/video7','same',123]]
  self.rows=[dict(name='cam_left_wrist',label='A',device='/dev/path',connected=True,status='ready')]
  self.value=encode('run','nonce','request',self.fp,self.rows)
 def parse(self,value=None,run='run',nonce='nonce',request='request',fp=None):return decode(self.value if value is None else value,run,nonce,request,self.fp if fp is None else fp)
 def test_valid_bound_result(self):self.assertEqual(self.parse(),self.rows)
 def test_old_run_nonce_request_and_device_generation_rejected(self):
  for kw in ({'run':'old'},{'nonce':'old'},{'request':'old'},{'fp':[['wrist','/dev/path','/dev/video7','same',124]]}):
   with self.subTest(kw=kw),self.assertRaises(ValueError):self.parse(**kw)
 def test_truncated_oversize_and_bad_checksum_rejected(self):
  for data in (self.value[:-2],self.value+'x'*MAX_BYTES,self.value.replace('/dev/path','/dev/evil')):
   with self.assertRaises(ValueError):self.parse(data)
 def test_duplicate_device_rows_rejected_even_with_valid_checksum(self):
  with self.assertRaises(ValueError):self.parse(encode('run','nonce','request',self.fp,self.rows*2))
 def test_duplicate_json_key_rejected_even_when_values_match(self):
  duplicate=self.value.replace('"schema":1','"schema":1,"schema":1')
  self.assertNotEqual(duplicate,self.value)
  with self.assertRaises(ValueError):self.parse(duplicate)
 def test_substituted_wrist_path_is_never_ready(self):
  rows=[dict(self.rows[0],device='/dev/other')]
  with self.assertRaises(ValueError):self.parse(encode('run','nonce','request',self.fp,rows))
 def test_headband_status_without_fresh_frame_is_not_ready(self):
  rows=[dict(name='cam_ros_bleft',label='bleft',connected=True,status='ready')]
  with self.assertRaises(ValueError):self.parse(encode('run','nonce','request',self.fp,rows))
  rows[0].update(fresh_frame=True,probe_scope='camd_snapshot')
  self.assertEqual(self.parse(encode('run','nonce','request',self.fp,rows)),rows)
if __name__=='__main__':unittest.main(verbosity=2)
