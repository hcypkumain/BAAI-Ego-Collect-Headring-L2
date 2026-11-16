import json, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import recording_store
class PreserveTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
  self.base=Path(self.tmp.name); self.volume=self.base/'volume';self.root=self.volume/'data'/'session';self.root.mkdir(parents=True)
  self.take=self.root/'sample';self.take.mkdir();self.store=recording_store.RecordingStore(self.base,self.base,SimpleNamespace(),lambda:self.root,lambda:self.volume)
  (self.take/'meta.json').write_text('{}');(self.take/'discard_request.json').write_text(json.dumps({'session_id':'sample','directory':str(self.take.resolve())}));(self.take/'stop_gate.json').write_text('{"recording_active":false}');(self.take/'start_gate.json').write_text('{}');(self.take/'data.bin').write_bytes(b'original capture')
 def check_preserved(self,summary):
  if summary is not None:(self.take/'native_capture_summary.json').write_text(summary)
  with self.assertRaisesRegex(ValueError,'保留'): self.store.discard_current(self.take)
  self.assertEqual((self.take/'data.bin').read_bytes(),b'original capture')
  self.assertFalse(self.store.operations.exists())
 def test_disconnect(self):self.check_preserved(json.dumps({'status':'error','errors':['emg_right: serial device disconnected']}))
 def test_discard_overwrote_final_status(self):self.check_preserved(json.dumps({'status':'discarded','capture_status':'error'}))
 def test_unreadable(self):self.check_preserved('{bad json')
 def test_missing(self):self.check_preserved(None)
 def test_final_summary_cannot_replace_missing_native_proof(self):
  (self.take/'capture_summary.json').write_text('{"status":"discarded"}')
  self.check_preserved(None)
 def test_keep_active_gate(self):
  (self.take/'stop_gate.json').write_text('{"recording_active":true}')
  with self.assertRaisesRegex(ValueError,'尚未确认停止'):self.store.discard_current(self.take)
  self.assertTrue((self.take/'data.bin').exists())
if __name__=='__main__':unittest.main()
