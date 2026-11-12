"""Registry caller boundaries: synthetic inputs only, never real SSH or USB."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
import subprocess
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import fleet, imports, metrics, online, operations, web_config
from host.qc.tests.governance_fixtures import sample_registry


class CallerTests(unittest.TestCase):
    def setUp(self):
        self.registry=sample_registry(('cp_0042','cp_0014'))
        self.device='cp_0042'
        self.config=operations.bound_config(web_config.setup(registry=self.registry,
            selected=[self.device],config={'ssh_config':'/synthetic/config'}),self.registry)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.state=Path(self.tmp.name)

    def calls(self,config=None):
        c=self.config if config is None else config;d=self.device
        return [lambda:metrics.collect(c,d),lambda:metrics.deep_check(c,d),
            lambda:metrics.create_plan(c,d,self.state),
            lambda:metrics.fetch_thumbnails(c,d,'emg',1,['image_x'],self.state),
            lambda:metrics.fetch_original(c,d,'emg',1,'image_'+'a'*40,self.state),
            lambda:metrics.audit_history(c,d),lambda:imports.remote(c,d,{'op':'scan'})]

    def plan(self):
        return fleet.bind_registry_plan({'schema':1,'operation':'import_tasks','device':self.device,
            'address':self.config['devices'][self.device],'created_at':time.time(),
            'remove_list_ids':[],'scan':'a'*32,'mode':'emg','context':'emg:5',
            'revision':1,'entries':[],'expected':{}},registry=self.registry,device=self.device)

    def test_all_seven_helpers_reach_actual_strict_registry_argv(self):
        identity=fleet.registry_device_identity(self.registry,self.device)
        seen=[]
        case=self
        class Stop(Exception):pass
        class SSH:
            def __init__(self,*args):
                seen.append(args);self.argv=['ssh','-F',str(args[0]),args[1]]
            def run(self,*args):
                case.assertEqual(self.argv[-1],identity['ssh_alias'])
                for key in ('address','ssh_user','host_key_alias'):
                    option={'address':'hostname','ssh_user':'user','host_key_alias':'hostkeyalias'}[key]
                    case.assertIn(option+'='+identity[key],self.argv)
                case.assertIn('stricthostkeychecking=yes',self.argv)
                raise Stop()
        def process(argv,**kw):
            self.assertEqual(argv[-2],identity['ssh_alias'])
            for value in ('hostname='+identity['address'],'user='+identity['ssh_user'],
                          'hostkeyalias='+identity['host_key_alias'],'stricthostkeychecking=yes'):
                self.assertIn(value,argv)
            raise Stop()
        with (patch.object(online,'config_for',return_value={'user':identity['ssh_user'],
                'hostkeyalias':identity['host_key_alias']}),patch.object(online,'SSH',SSH),
                patch.object(metrics.subprocess,'run',side_effect=process)):
            for call in self.calls():
                with self.assertRaises(Stop):call()
        self.assertEqual(len(seen),7)
        self.assertTrue(all(args[1:3]==(identity['ssh_alias'],identity['address']) for args in seen))
        self.assertNotEqual(identity['ssh_alias'],self.device)

    def test_bad_configs_all_helpers_zero_transport_process_and_files(self):
        bad=[]
        for key in ('_registry','registry_digest','identities'):
            c=copy.deepcopy(self.config);c.pop(key);bad.append(c)
        for key in ('ssh_alias','ssh_user','host_key_alias'):
            c=copy.deepcopy(self.config);c['_registry']=self.registry
            c['identities'][self.device][key]='malicious';bad.append(c)
        c=copy.deepcopy(self.config);c['_registry']=self.registry;c['devices'][self.device]='192.0.2.250';bad.append(c)
        c=operations.bound_config(web_config.setup(registry=self.registry,selected=['cp_0014'],
            config={'ssh_config':'synthetic'}),self.registry);bad.append(c)
        with patch.object(online,'SSH') as ssh,patch('subprocess.Popen') as popen,patch('subprocess.run') as run:
            for config in bad:
                for call in self.calls(config):
                    with self.assertRaises((ValueError,KeyError)):call()
            ssh.assert_not_called();popen.assert_not_called();run.assert_not_called()
        self.assertEqual(list(self.state.iterdir()),[])

    def test_create_plan_bound_before_actual_hash_and_apply_boundary_accepts(self):
        ssh=Mock();ssh.run.return_value=b'cp_0042\n'
        plan={'device':self.device,'diff':[],'protected':{},'component_diff':[]}
        with (patch.object(operations,'caller_ssh',return_value=ssh),patch.object(fleet,'build',
                return_value={'manifest_sha256':'a'*64}),patch.object(online,'read_live',return_value={}),
                patch.object(online,'prepare_plan',return_value=plan)):
            result=metrics.create_plan(self.config,self.device,self.state)
        stored=online.load_plan(self.state,result['plan_sha256'])
        fleet.require_registry_plan(stored,registry=self.registry,device=self.device)
        self.assertEqual(stored['registry_device'],fleet.registry_device_identity(self.registry,self.device))
        for key in ('registry_digest','registry_device'):
            broken=dict(stored);broken.pop(key)
            with self.assertRaises(ValueError):fleet.require_registry_plan(broken,registry=self.registry,device=self.device)

    def test_import_bad_plan_bindings_zero_marker_remote_and_process(self):
        variants=[]
        for key in ('registry_digest','registry_device'):
            plan=self.plan();plan.pop(key);variants.append(plan)
        plan=self.plan();plan['registry_digest']='f'*64;variants.append(plan)
        for key in ('device_id','address','ssh_alias','ssh_user','host_key_alias'):
            plan=self.plan();plan['registry_device'][key]='malicious';variants.append(plan)
        with patch.object(imports,'remote') as remote,patch('subprocess.Popen') as popen,patch('subprocess.run') as run:
            for plan in variants:
                digest=imports.store(self.state/'imports/plans'/('temp'+str(len(list(self.state.rglob('temp*'))))),plan)
                imports.store(self.state/'imports/plans'/(digest+'.json'),plan)
                with self.assertRaises(ValueError):imports.execute(self.state,self.config,self.device,digest)
            remote.assert_not_called();popen.assert_not_called();run.assert_not_called()
        self.assertFalse((self.state/'imports/started').exists())

    def test_scan_and_preview_binding_and_drift_before_write(self):
        sid='a'*32;zid='b'*32
        scan={'status':'ok','mode':'emg','context':'emg:5','scan':{'scan_id':sid,
            'catalog_revision':1,'files':[{'id':zid,'relative_path':'a.zip','size_bytes':1}]}}
        with patch.object(imports,'remote',return_value=scan):imports.scan(self.state,self.config,self.device)
        record=imports.load(self.state/'imports/scans'/(sid+'.json'))
        fleet.require_registry_plan(record,registry=self.registry,device=self.device)
        record['registry_device']['ssh_alias']='evil'
        path=self.state/'imports/scans'/(sid+'.json');path.write_bytes(imports.canonical(record))
        with patch.object(imports,'remote') as remote:
            with self.assertRaises(ValueError):imports.preview(self.state,self.config,self.device,sid,[zid])
            remote.assert_not_called()
        plan=self.plan();digest=imports.store(self.state/'imports/plans'/('valid.json'),plan)
        imports.store(self.state/'imports/plans'/(digest+'.json'),plan)
        def drift(stage):self.config['identities'][self.device]['ssh_user']='evil'
        with patch.object(imports,'remote') as remote:
            with self.assertRaises(ValueError):imports.execute(self.state,self.config,self.device,digest,drift)
            remote.assert_not_called()
        self.assertFalse((self.state/'imports/started').exists())

    def test_trust_identity_mismatch_zero_SSH(self):
        with patch.object(online,'config_for',return_value={'user':'wrong','hostkeyalias':'wrong'}),patch.object(online,'SSH') as ssh:
            for call in self.calls():
                with self.assertRaises(online.Failure):call()
            ssh.assert_not_called()

    def test_valid_preview_hash_bound_and_post_remote_drift_no_write(self):
        sid='a'*32;zid='b'*32
        record=fleet.bind_registry_plan({'device':self.device,'address':self.config['devices'][self.device],
            'created_at':time.time(),'mode':'emg','context':'emg:5','revision':1,
            'entries':[{'id':zid,'relative_path':'a.zip'}]},registry=self.registry,device=self.device)
        imports.store(self.state/'imports/scans'/(sid+'.json'),record)
        info={'status':'preview','new_unique':1,'reused_unique':0,'name_conflicts':0,
            'existing_lists':1,'available_bytes':100,'estimated_bytes':1,'volume_majmin':'8:1','sources':[]}
        with patch.object(imports,'remote',return_value=info):
            result=imports.preview(self.state,self.config,self.device,sid,[zid])
        plan=imports.load(self.state/'imports/plans'/(result['plan_sha256']+'.json'))
        fleet.require_registry_plan(plan,registry=self.registry,device=self.device)
        self.assertEqual(plan['remove_list_ids'],[])
        def drift(*args,**kwargs):
            self.config['registry_digest']='f'*64
            return info
        with patch.object(imports,'remote',side_effect=drift),patch.object(imports,'store') as store:
            with self.assertRaises(ValueError):imports.preview(self.state,self.config,self.device,sid,[zid])
            store.assert_not_called()

    def test_scan_post_remote_drift_and_execute_config_drift_zero_writes(self):
        sid='a'*32
        def drift(*args,**kwargs):
            self.config['identities'][self.device]['host_key_alias']='evil'
            return {'status':'ok','mode':'emg','context':'emg:5',
                    'scan':{'scan_id':sid,'catalog_revision':1,'files':[]}}
        with patch.object(imports,'remote',side_effect=drift),patch.object(imports,'store') as store:
            with self.assertRaises(ValueError):imports.scan(self.state,self.config,self.device)
            store.assert_not_called()
        with patch.object(imports,'remote') as remote,patch.object(imports,'load') as load,patch('subprocess.Popen') as popen:
            with self.assertRaises(ValueError):imports.execute(self.state,self.config,self.device,'a'*64)
            remote.assert_not_called();load.assert_not_called();popen.assert_not_called()
        self.assertEqual(list(self.state.iterdir()),[])

    def malformed_records(self,record):
        raw=json.dumps(record,separators=(',',':')).encode()
        for token in (b'NaN',b'Infinity',b'-Infinity',b'1e999',b'true',b'"recent"'):
            changed=dict(record);changed['created_at']='TIMESTAMP'
            yield json.dumps(changed,separators=(',',':')).encode().replace(b'"TIMESTAMP"',token)
        yield raw.replace(b'"registry_digest":',b'"registry_digest":"wrong","registry_digest":',1)

    def test_strict_execute_malformed_hashed_records_zero_marker_or_remote(self):
        with patch.object(imports,'remote') as remote,patch('subprocess.Popen') as popen:
            for raw in self.malformed_records(self.plan()):
                digest=hashlib.sha256(raw).hexdigest()
                path=self.state/'imports/plans'/(digest+'.json');path.parent.mkdir(parents=True,exist_ok=True)
                path.write_bytes(raw)
                with self.subTest(raw=raw[-100:]),self.assertRaises(ValueError):
                    imports.execute(self.state,self.config,self.device,digest)
            remote.assert_not_called();popen.assert_not_called()
        self.assertFalse((self.state/'imports/started').exists())

    def test_strict_scan_record_preview_malformed_zero_remote_or_plans(self):
        sid='a'*32;zid='b'*32
        record=fleet.bind_registry_plan({'device':self.device,'address':self.config['devices'][self.device],
            'created_at':time.time(),'entries':[{'id':zid,'relative_path':'a.zip'}],
            'mode':'emg','context':'emg:5','revision':1},registry=self.registry,device=self.device)
        path=self.state/'imports/scans'/(sid+'.json');path.parent.mkdir(parents=True)
        with patch.object(imports,'remote') as remote,patch('subprocess.Popen') as popen:
            for raw in self.malformed_records(record):
                path.write_bytes(raw)
                with self.subTest(raw=raw[-100:]),self.assertRaises(ValueError):
                    imports.preview(self.state,self.config,self.device,sid,[zid])
            remote.assert_not_called();popen.assert_not_called()
        self.assertFalse((self.state/'imports/plans').exists())

    def test_strict_IPC_plain_import_metrics_and_assets(self):
        bad=[b'{"status":"failed","status":"succeeded"}',b'{"revision":NaN}',
             b'{"revision":Infinity}',b'{"revision":1e999}',
             b'{"identity":"cp_0043","identity":"cp_0042","revision":NaN}',b'{bad}']
        transport=Mock();transport.argv=['ssh','synthetic-alias']
        with patch.object(operations,'caller_ssh',return_value=transport):
            for raw in bad:
                with patch('subprocess.run',return_value=type('Result',(),{'returncode':0,'stdout':raw})()):
                    for call in (lambda:imports.remote(self.config,self.device,{'op':'scan'}),
                            lambda:metrics.collect(self.config,self.device),
                            lambda:metrics.fetch_thumbnails(self.config,self.device,'emg',1,['image_x'],self.state),
                            lambda:metrics.fetch_original(self.config,self.device,'emg',1,'image_'+'a'*40,self.state)):
                        with self.subTest(raw=raw),self.assertRaises(ValueError):call()
        self.assertFalse((self.state/'assets').exists())

    def test_strict_stream_each_line_real_local_process(self):
        popen=subprocess.Popen
        transport=Mock();transport.argv=['ssh','synthetic-alias']
        for raw in (b'{"status":"failed","status":"succeeded"}',b'{"revision":NaN}',
                    b'{"event":"verifying","event":"building:1:1"}',b'{"revision":Infinity}'):
            processes=[];events=[]
            def local(argv,**kwargs):
                p=popen([sys.executable,'-I','-S','-c',
                    'import sys;sys.stdin.buffer.read();sys.stdout.buffer.write('+repr(raw+b'\n')+');sys.stdout.flush()'],**kwargs)
                processes.append(p);return p
            with patch.object(operations,'caller_ssh',return_value=transport),patch('subprocess.Popen',side_effect=local):
                with self.subTest(raw=raw),self.assertRaises(ValueError):
                    imports.remote(self.config,self.device,{'op':'apply'},timeout=5,on_event=events.append)
            self.assertEqual(events,[])
            self.assertTrue(all(p.poll() is not None for p in processes))

    def test_canonical_finite_unchanged_encoding_and_safe_reason(self):
        value={'unicode':'示例','created_at':time.time()}
        expected=(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n').encode()
        self.assertEqual(imports.canonical(value),expected)
        for value in (float('nan'),float('inf'),float('-inf')):
            with self.assertRaises(ValueError):imports.canonical({'value':value})
        self.assertEqual(imports.safe_reason('/private/remote/error/token'),'remote_failed')
        self.assertEqual(imports.safe_reason('catalog_changed'),'catalog_changed')

    def test_execute_hashes_same_bounded_bytes_and_preserves_time_policy(self):
        plan=self.plan();raw=imports.canonical(plan);digest=hashlib.sha256(raw).hexdigest()
        with patch.object(imports,'load_bytes',return_value=raw) as read,\
                patch.object(Path,'read_bytes',side_effect=AssertionError('second state read')),\
                patch.object(imports,'remote',return_value={'status':'failed','reason':'/private/unverified'}) as remote:
            result=imports.execute(self.state,self.config,self.device,digest)
        read.assert_called_once();remote.assert_called_once()
        self.assertEqual(result,{'status':'failed','reason':'remote_failed'})
        with patch.object(imports.time,'time',return_value=1000):
            self.assertFalse(imports.expired(400));self.assertTrue(imports.expired(399))
            self.assertFalse(imports.expired(1001))  # Preserve existing future policy.
            for value in (True,False,None,'1000',float('nan'),float('inf')):
                with self.assertRaises(ValueError):imports.expired(value)

    def test_state_load_is_bounded_regular_nonsymlink(self):
        path=self.state/'state.json';path.write_bytes(b'{"ok":true}')
        self.assertEqual(imports.load(path),{'ok':True})
        with self.assertRaises(ValueError):imports.load(path,limit=2)
        link=self.state/'link';link.symlink_to(path)
        with self.assertRaises((OSError,ValueError)):imports.load(link)
        with self.assertRaises((OSError,ValueError)):imports.load(self.state)
