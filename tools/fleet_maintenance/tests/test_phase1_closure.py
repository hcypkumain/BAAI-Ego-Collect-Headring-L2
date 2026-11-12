import ast,importlib.util,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
spec=importlib.util.spec_from_file_location('probe_closure',ROOT/'resources/metrics_probe.py');probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
class Closure(unittest.TestCase):
 def catalog(self):
  return {'revision':4,'images':{'image_'+'a'*40:{'path':'dir/a.png','deleted':False,'name':'a'},'image_'+'b'*40:{'path':'dir/b.png','deleted':False,'name':'b'}},'list_order':['task_a','task_b'],'lists':{'task_a':{'name':'one','deleted':False,'items':['image_'+'a'*40]},'task_b':{'name':'two','deleted':False,'items':['image_'+'a'*40,'image_'+'b'*40]}}}
 def test_list_dedup_and_history_exact_path(self):
  cat=self.catalog();r=probe.offline_reference(cat);a=r['items'][0];self.assertEqual(a['lists'],['task_a','task_b']);self.assertIsNone(a['completed'])
  paths=probe.catalog_membership(r,cat); rec={('emg','s'):{'id':'s','status':'pending_qc','reference_path':'configs/environments/dir/a.png'},('emg','s2'):{'id':'s2','reference_path':'other/a.png'}}
  h=probe.history_rows(rec,paths,'emg');self.assertEqual(h[0]['asset_id'],'image_'+'a'*40);self.assertIsNone(h[1]['asset_id']);self.assertFalse(h[0]['saved'])
 def test_catalog_revision_drift(self):
  r={'revision':5,'items':[]};self.assertEqual(probe.catalog_membership(r,self.catalog()),{});self.assertEqual(r['membership_status'],'revision_mismatch_or_unavailable')
 def test_block_usb_inherited(self):
  d={'blockdevices':[{'name':'sda','type':'disk','tran':'usb','children':[{'name':'sda1','maj:min':'8:1','type':'part'}]}]};v=[{'majmin':'8:1','aliases':['/run/media/usb']}];r=probe.block_inventory(d,v);self.assertTrue(r[1]['usb']);self.assertTrue(r[1]['mounted']);self.assertEqual(r[1]['aliases'],['/run/media/usb'])
 def test_secure_open_rejects_symlink(self):
  tree=ast.parse((ROOT/'resources/original_probe.py').read_text());func=next(x for x in tree.body if isinstance(x,ast.FunctionDef) and x.name=='open_safe');ns={'os':os};exec(compile(ast.Module(body=[func],type_ignores=[]),'safe','exec'),ns)
  with tempfile.TemporaryDirectory() as t:
   p=Path(t);(p/'real').write_text('x');(p/'link').symlink_to(p/'real')
   with self.assertRaises(OSError):ns['open_safe'](p/'link')
   fd=ns['open_safe'](p/'real');os.close(fd)
 def test_pixel_bomb_rejected_before_decode(self):
  import metrics,json,base64,hashlib
  raw=b'fake'; ident='image_'+'a'*40
  value={'id':ident,'data':base64.b64encode(raw).decode(),'sha256':hashlib.sha256(raw).hexdigest()}
  class Proc: returncode=0;stdout=json.dumps(value).encode()
  class SSH:
   def __init__(self,*a):self.argv=['ssh','cp_0014']
  class Big:
   format='PNG';width=10000;height=10000
   def __enter__(self):return self
   def __exit__(self,*a):pass
   def verify(self):raise AssertionError('must reject before verify')
  with tempfile.TemporaryDirectory() as t,patch.object(metrics.online,'SSH',SSH),patch.object(metrics.subprocess,'run',return_value=Proc()),patch('PIL.Image.open',return_value=Big()):
   with self.assertRaises(ValueError):metrics.fetch_original({'devices':{'cp_0014':'192.168.20.111'},'ssh_config':'x'},'cp_0014','eeg',1,ident,t)
 def test_inactive_gate_uses_active_privacy(self):
  text=(ROOT/'resources/original_probe.py').read_text();self.assertIn("gate.get('collection_mode')!=selection.get('active_mode')",text);self.assertIn("base=code if mode==selection['legacy_mode']",text)
if __name__=='__main__':unittest.main()
