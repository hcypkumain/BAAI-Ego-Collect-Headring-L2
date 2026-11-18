import ast,json,unittest,tempfile
from pathlib import Path
from types import SimpleNamespace
p=Path(__file__).resolve().parents[1]/'src/orangepi_collect/storage/ledger.py';tree=ast.parse(p.read_text());nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ('_session_matches_image','reserved_used')];fake_yaml=SimpleNamespace(YAMLError=ValueError);ns={'Path':Path,'yaml':fake_yaml};exec(compile(ast.fix_missing_locations(ast.Module(body=nodes,type_ignores=[])),'ledger','exec'),ns)
class Test(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.library=self.root/'configs/environments';self.library.mkdir(parents=True);self.pic=self.library/'x_1.png';self.pic.write_bytes(b'x');self.session=self.root/'session';self.session.mkdir();(self.session/'meta.yaml').write_text('{"status":"pending_qc","reference_image":"configs/environments/x_1.png"}');self.entry={'status':'pending_qc','session':str(self.session)};self.calls=0;ns['LIBRARY']=self.library;ns['PROJECT']=self.root;ns['_read_json']=lambda p:{'status':'pending_qc','storage_durability':{'completed':True}}
  def parse(s):self.calls+=1;return json.loads(s)
  ns['_load_yaml']=parse
 def test_one_metadata_parse_and_correct_binding(self):
  self.assertTrue(ns['reserved_used'](self.entry,self.pic));self.assertEqual(self.calls,1)
 def test_wrong_or_missing_reference_rejected(self):
  self.assertFalse(ns['reserved_used'](self.entry,self.library/'other.png'));(self.session/'meta.yaml').unlink();self.assertFalse(ns['reserved_used'](self.entry,self.pic))
 def test_invalid_proof_or_deleted_not_reserved(self):
  ns['_read_json']=lambda p:{'status':'pending_qc','storage_durability':{'completed':False}};self.assertFalse(ns['reserved_used'](self.entry,self.pic));(self.session/'recording_deleted.json').write_text('{}');self.assertFalse(ns['reserved_used'](self.entry,self.pic))
 def test_unstable_metadata_rejected(self):
  def parse(s):
   (self.session/'meta.yaml').write_text(s+' ');return json.loads(s)
  ns['_load_yaml']=parse;self.assertFalse(ns['reserved_used'](self.entry,self.pic))
unittest.main()
