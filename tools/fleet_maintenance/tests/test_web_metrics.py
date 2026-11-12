import importlib.util, os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
spec=importlib.util.spec_from_file_location('metrics_probe_test',ROOT/'resources/metrics_probe.py'); probe=importlib.util.module_from_spec(spec); spec.loader.exec_module(probe)
import metrics, fleet

class ProbeTests(unittest.TestCase):
    def fixture(self, endpoints):
        def fetch(url):
            self.assertTrue(url.startswith(('reference-library?','reference-list?')))
            return endpoints.get(url,(None,'missing'))
        return fetch
    def library(self,lists,revision=1,next_offset=None): return {'revision':revision,'lists':[{'id':x,'deleted':False} for x in lists],'next_offset':next_offset}
    def listing(self,items,revision=1,next_offset=None): return {'revision':revision,'items':items,'next_offset':next_offset}
    def item(self,id,completed,deleted=False): return {'id':id,'completed':completed,'deleted':deleted}
    def test_all_virtual_dedup_and_deleted(self):
        f=self.fixture({'reference-library?offset=0&limit=50':(self.library(['all']),None),'reference-list?id=all&offset=0&limit=50':(self.listing([self.item('a',True),self.item('a',True),self.item('b',False),self.item('c',True,True)]),None)})
        r=probe.reference(f); self.assertEqual((r['total_unique'],r['completed_unique'],r['remaining_unique']),(2,1,1)); self.assertIsNone(r['pending_unique']); self.assertTrue(r['partial'])
    def test_multiple_lists_deduplicate(self):
        a='list_'+'a'*32; b='list_'+'b'*32
        f=self.fixture({'reference-library?offset=0&limit=50':(self.library([a,b]),None),f'reference-list?id={a}&offset=0&limit=50':(self.listing([self.item('shared',False)]),None),f'reference-list?id={b}&offset=0&limit=50':(self.listing([self.item('shared',True),self.item('other',False)]),None)})
        r=probe.reference(f); self.assertEqual((r['total_unique'],r['completed_unique']),(2,1)); self.assertIsNone(r['pending_unique'])
    def test_limit_is_partial_and_unknown_when_no_item_page(self):
        r=probe.reference(self.fixture({'reference-library?offset=0&limit=50':(self.library(['all']),None)}),max_pages=1)
        self.assertIn('limit',r['reason']); self.assertIsNone(r['total_unique'])
    def test_truncated_pages_keep_partial_subtotal(self):
        f=self.fixture({'reference-library?offset=0&limit=50':(self.library(['all']),None),'reference-list?id=all&offset=0&limit=50':(self.listing([self.item('a',False)],next_offset=50),None)})
        r=probe.reference(f,max_pages=2); self.assertEqual(r['total_unique'],1); self.assertEqual(r['completed_unique'],0); self.assertIn('limit',r['reason']); self.assertTrue(r['partial'])
    def test_missing_next_offset_not_zero(self):
        r=probe.reference(lambda _: ({'revision':1,'lists':[]},None)); self.assertIsNone(r['total_unique']); self.assertIn('invalid_pagination',r['reason'])
    def test_revision_drift_never_complete(self):
        f=self.fixture({'reference-library?offset=0&limit=50':(self.library(['all']),None),'reference-list?id=all&offset=0&limit=50':(self.listing([],revision=2),None)})
        r=probe.reference(f); self.assertIn('revision_changed',r['reason']); self.assertIsNone(r['total_unique'])
    def test_auth_error_is_unknown_not_zero(self):
        r=probe.reference(lambda _: (None,'auth_denied')); self.assertIsNone(r['total_unique']); self.assertEqual(r['reason'],'auth_denied')
    def test_volume_aliases_usb_and_automount(self):
        lines=['1 0 8:1 / / rw - ext4 /dev/a rw','2 0 8:2 / /media/card rw - ext4 /dev/b rw','3 0 8:2 / /run/media/card\\040one rw - ext4 /dev/b rw','4 0 8:3 / /run/collector-usb-task-import/usb rw - vfat /dev/c rw','5 0 0:5 / /run/collector-usb-task-import/auto rw - autofs systemd rw','6 0 8:4 / /mnt/unmounted rw - ext4 /dev/d rw']
        class Stat: pass
        class V: f_blocks=100;f_bfree=20;f_bavail=10;f_frsize=1024
        calls=[]
        def stat(p):
            calls.append(p); s=Stat(); s.st_dev=os.makedev(8, {'/':1,'/media/card':2,'/run/media/card one':2,'/run/collector-usb-task-import/usb':3,'/mnt/unmounted':1}[p]); return s
        r=probe.volumes_from_mountinfo(lines,{'8:2':'uuid'},stat,lambda _:V())
        self.assertEqual(len(r),3); self.assertEqual(r[1]['aliases'],['/media/card','/run/media/card one']); self.assertEqual(r[1]['used'],81920); self.assertNotIn('/run/collector-usb-task-import/auto',calls)
    def test_saved_pending_not_qualified(self):
        self.assertTrue(probe.saved({'retained':True,'has_data':True,'status':'pending_qc','qualified':False}))
    def test_no_credential_in_reference_result(self):
        r=probe.reference(lambda _: (None,'auth_denied')); self.assertNotIn('token',str(r)); self.assertNotIn('key',str(r))
    def test_deep_check_public_fields_and_readonly(self):
        manifest=__import__('json').loads((fleet.RESOURCES/'manifest.json').read_bytes())
        live={'device_id':'cp_0014','project':__import__('online').PROJECT,'files':{x['path']:{'sha256':x['sha256']} for x in manifest['files']},'protected':{'/secret/calibration':{'type':'regular','sha256':'secret-hash'}},'component_timer':{}}
        class SSH:
            def __init__(self,*a): self.argv=['ssh',a[1]]
            def run(self,cmd): self_cmds.append(cmd); return b'cp_0014\n'
        self_cmds=[]
        from host.qc.tests.governance_fixtures import sample_registry
        import operations,web_config
        registry=sample_registry(('cp_0014',))
        config=operations.bound_config(web_config.setup(registry=registry,selected=['cp_0014'],config={'ssh_config':'x'}),registry)
        with patch.object(metrics.online,'SSH',SSH),patch.object(metrics.online,'config_for',return_value={'user':'fixture','hostkeyalias':'test-key-cp_0014'}),patch.object(metrics.online,'read_live',return_value=live) as read:
            r=metrics.deep_check(config,'cp_0014')
        self.assertEqual(self_cmds,['cat /opt/egorecord/DEVICE_ID']); read.assert_called_once(); self.assertTrue(r['code_matches_pin']); self.assertNotIn('secret',str(r)); self.assertEqual(r['calibration']['regular_files'],1)
if __name__=='__main__': unittest.main()
