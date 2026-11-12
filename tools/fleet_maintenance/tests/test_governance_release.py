"""Offline immutable Git resources and synthetic registry/observation tests only."""
from copy import deepcopy
from dataclasses import asdict
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import sys
import unittest
from unittest.mock import patch

# Legacy transport modules use bare sibling imports; production path setup is main-owned.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fleet
import delta_release
import online
import web_config
from host.common.governance_contract import COMPONENT_IDS
from host.common.strict_json import canonical_json
from host.qc.tests.governance_fixtures import sample_registry, sample_probe_result, sample_evidence
from tools.fleet_maintenance.component_bom import build_bom, compare_components
from tools.fleet_maintenance.release_registry import (
    PIN, OVERLAY_ID, OVERLAY_PINS, overlay_payload, resource_digest, validate_release)

C0 = '1404ba567009058e40d4294f8555a66e5ee8ca86'
ROOT = Path(__file__).resolve().parents[3]
PREFIX = 'tools/fleet_maintenance/resources/'

def blob(ref, path):
    return subprocess.run(['git', '-C', str(ROOT), 'show', ref+':'+path],
                          check=True, capture_output=True).stdout

def approval(scope):
    return {'status': 'approved', 'scope': list(scope), 'reason': 'synthetic offline approval',
            'expiry': '2099-01-01T00:00:00Z'}

def record(component_id, files=None):
    return {'component_id': component_id, 'files': files or [], 'approval': approval([component_id])}

def make_release(bom, resources, scope):
    a = approval(scope)
    release = {'release_id': 'synthetic-host-or-delta', 'base_commit': bom['base_commit'],
               'overlay_ids': [o['overlay_id'] for o in bom['overlays']], 'component_BOM': bom,
               'scope': list(scope), 'reason': a['reason'], 'expiry': a['expiry'], 'approval': a,
               'resource_digest': resource_digest(resources)}
    return release, {release['release_id']: release['resource_digest']}

class GovernanceRelease(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stable_raw = blob(C0, PREFIX+'manifest.json')
        cls.stable = json.loads(cls.stable_raw)
        cls.resources = {'stable_manifest.json': cls.stable_raw}
        cls.resources.update({k: blob(C0, PREFIX+OVERLAY_ID+'/'+k) for k in OVERLAY_PINS})
        cls.delta, cls.payload = overlay_payload(cls.resources)
        cls.base_bytes = {f['path']: blob(PIN, f['path']) for f in cls.stable['files']}
        cls.source = {'schema_version': 1, 'base_commit': PIN,
                      'components': [record(c) for c in COMPONENT_IDS]}
        next(r for r in cls.source['components'] if r['component_id']=='collector')['files'] = [
            {'ref': PIN, 'path': f['path'], 'sha256': f['sha256'],
             'provenance': 'certified:f5', 'resource': f['path']} for f in cls.stable['files']]
        cls.overlay = {'overlay_id': OVERLAY_ID, 'ref': C0, 'approval': approval(['collector']),
                       'resources': {k: PREFIX+OVERLAY_ID+'/'+k for k in OVERLAY_PINS}}
        cls.overlay['resources']['stable_manifest.json'] = PREFIX+'manifest.json'
        def reader(ref, path):
            if ref == PIN:
                return cls.base_bytes[path]
            return blob(ref, path)
        cls.bom = build_bom(source_manifest=cls.source, read_blob=reader, overlays=[cls.overlay])
        cls.all_resources = {**cls.resources, **cls.base_bytes,
                             **{'payload/'+p: b for p,b in cls.payload.items()}}

    def release(self):
        return make_release(deepcopy(self.bom), self.all_resources, ['collector'])

    def codes(self, release, router=None, resources=None):
        if router is None:
            router = {release['release_id']: release['resource_digest']}
        return validate_release(release=release, router=router,
                                resources=self.all_resources if resources is None else resources)

    def test_real_fixed_overlay_reconstructs_five(self):
        release, router = self.release()
        self.assertEqual(self.codes(release, router), ())
        collector = next(c for c in self.bom['components'] if c['component_id']=='collector')
        after = {f['path']:f for f in collector['source']['files']}
        self.assertEqual(len(after), 236)
        for item in self.delta['files']:
            self.assertEqual(after[item['path']]['sha256'], item['sha256'])
            self.assertNotEqual(item['before_sha256'], item['sha256'])
            self.assertEqual(after[item['path']]['ref'], C0)
        self.assertEqual(len(self.payload), 5)

    def test_all_nine_are_coverage_not_installation(self):
        self.assertEqual({c['component_id'] for c in self.bom['components']}, set(COMPONENT_IDS))
        for c in self.bom['components']:
            self.assertEqual(c['installed'], {'sha256':None,'status':'unknown','evidence':None})
            self.assertEqual(c['runtime'], c['installed'])
        phone = next(c for c in self.bom['components'] if c['component_id']=='phone_link')
        self.assertIsNone(phone['source']['sha256'])

    def test_missing_phone_or_helper_blocks(self):
        for name in ('phone_link', 'phone_media', 'helper'):
            release, _ = self.release()
            release['component_BOM']['components'] = [c for c in release['component_BOM']['components'] if c['component_id']!=name]
            self.assertIn('component_coverage', self.codes(release))

    def test_wrong_router_blocks(self):
        release, _ = self.release()
        self.assertIn('router_resource_mismatch', self.codes(release, {release['release_id']:'0'*64}))

    def test_missing_router_blocks(self):
        release, _ = self.release()
        self.assertIn('router_resource_mismatch', self.codes(release, {}))

    def test_resource_tamper_blocks(self):
        for name in self.resources:
            release, _ = self.release()
            resources = dict(self.all_resources); resources[name] += b'tamper'
            self.assertTrue(self.codes(release, resources=resources), name)

    def test_missing_each_fixed_resource_blocks(self):
        for name in self.resources:
            release, _ = self.release()
            resources = dict(self.all_resources); del resources[name]
            self.assertTrue(self.codes(release, resources=resources), name)

    def test_before_bytes_cannot_masquerade_as_after(self):
        release, _ = self.release()
        resources = dict(self.all_resources)
        path = self.delta['files'][0]['path']
        resources['payload/'+path] = self.base_bytes[path]
        release['resource_digest'] = resource_digest(resources)
        self.assertIn('source_resource_mismatch', self.codes(release, resources=resources))

    def test_wrong_baseline_blocks(self):
        release, _ = self.release()
        release['base_commit'] = release['component_BOM']['base_commit'] = 'a'*40
        self.assertIn('collector_baseline_unknown', self.codes(release))

    def test_unknown_base_blocks(self):
        release, _ = self.release(); release['base_commit'] = None
        self.assertIn('base_unknown', self.codes(release))

    def test_approval_scope_blocks(self):
        release, _ = self.release(); release['approval']['scope'] = ['monitor']
        self.assertIn('release_approval', self.codes(release))

    def test_component_approval_blocks(self):
        release, _ = self.release()
        next(c for c in release['component_BOM']['components'] if c['component_id']=='collector')['approval']['scope']=['helper']
        self.assertIn('component_approval_scope', self.codes(release))

    def test_overlay_approval_blocks(self):
        release, _ = self.release(); release['component_BOM']['overlays'][0]['approval']['status']='unknown'
        self.assertIn('overlay_approval', self.codes(release))

    def test_expired_approval_blocks(self):
        release, _ = self.release(); release['approval']['expiry']='2000-01-01T00:00:00Z'
        self.assertIn('release_approval', self.codes(release))

    def test_unknown_source_in_scope_blocks(self):
        release, router = make_release(deepcopy(self.bom), self.all_resources, ['phone_link'])
        self.assertIn('source_unknown', self.codes(release, router))

    def test_unregistered_orphans_block_but_host_release_passes(self):
        path='host/qc/fleet_monitor/__init__.py'; data=blob(C0,path)
        source={'schema_version':1,'base_commit':C0,'components':[record(c) for c in COMPONENT_IDS]}
        next(c for c in source['components'] if c['component_id']=='monitor')['files']=[
            {'ref':C0,'path':path,'sha256':fleet.sha(data),'resource':path,'provenance':'certified:host'}]
        bom=build_bom(source_manifest=source,read_blob=blob,overlays=[])
        release,router=make_release(bom,{path:data},['monitor'])
        self.assertEqual(validate_release(release=release,router=router,resources={path:data}),())
        for orphan in ('61f-unapproved-synthetic','7fcb-unapproved-synthetic'):
            bad=deepcopy(release);bad['overlay_ids']=[orphan]
            self.assertIn('overlay_unregistered',validate_release(release=bad,router=router,resources={path:data}))

    def test_source_mismatch_rejected_during_build(self):
        source=deepcopy(self.source); source['components'][0]['files'][0]['sha256']='0'*64
        with self.assertRaises(ValueError):
            build_bom(source_manifest=source,read_blob=lambda r,p:self.base_bytes[p],overlays=[])

    def test_symbolic_ref_rejected(self):
        source=deepcopy(self.source);source['base_commit']='HEAD'
        with self.assertRaises(ValueError):
            build_bom(source_manifest=source,read_blob=blob,overlays=[])

    def test_same_installed_runtime_unknown(self):
        collector=next(c for c in self.bom['components'] if c['component_id']=='collector')
        f=collector['source']['files'][0]
        entry={'collector':{'sha256':f['sha256'],'status':'observed','source':'file:'+f['path']}}
        obs=sample_probe_result(fields={'installed_components':sample_evidence(entry)})
        result=compare_components(self.bom,obs)
        actual=next(c for c in result['components'] if c['component_id']=='collector')
        self.assertTrue(actual['installed']['evidence']['matches_source'])
        self.assertEqual(actual['runtime']['status'],'unknown')
        self.assertEqual(collector['installed']['status'],'unknown')

    def test_runtime_plain_source_file_not_loaded_proof(self):
        f=next(c for c in self.bom['components'] if c['component_id']=='collector')['source']['files'][0]
        entry={'collector':{'sha256':f['sha256'],'status':'observed','source':'file:'+f['path']}}
        result=compare_components(self.bom,sample_probe_result(fields={'runtime_components':sample_evidence(entry)}))
        self.assertTrue(all(c['runtime']['status']=='unknown' for c in result['components']))

    def test_wrong_file_label_not_compared(self):
        entry={'collector':{'sha256':'a'*64,'status':'observed','source':'file:other.py'}}
        result=compare_components(self.bom,sample_probe_result(fields={'installed_components':sample_evidence(entry)}))
        self.assertTrue(all(c['installed']['status']=='unknown' for c in result['components']))

    def test_no_observation_is_unknown(self):
        result=compare_components(self.bom,None)
        self.assertTrue(all(c['runtime']['status']=='unknown' for c in result['components']))

    def test_five_plus_registry_only_42(self):
        registry=sample_registry()
        config=web_config.setup(registry=registry,selected=[d.device_id for d in registry.devices],config={'ssh_config':'/synthetic/ssh'})
        self.assertEqual(len(config['devices']),6)
        self.assertIn('cp_0042',config['devices'])

    def test_web_legacy_call_fails_closed(self):
        with self.assertRaises(TypeError):web_config.setup()

    def test_no_targets_parser_exits_before_io(self):
        with patch.object(Path,'read_text',side_effect=AssertionError('no read')), patch.object(online,'SSH',side_effect=AssertionError('no SSH')):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as e:
                fleet.main(['--registry','/synthetic/registry'])
            self.assertEqual(e.exception.code,2)

    def test_no_registry_parser_exits(self):
        with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
            fleet.main(['--targets','cp_0042'])

    def test_cli_registry_strict_duplicate_keys(self):
        with patch.object(Path,'read_text',return_value='{"schema_version":1,"schema_version":1,"devices":[]}'):
            with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
                fleet.main(['--registry','/synthetic/registry','--targets','cp_0042'])

    def test_cache_cannot_override_identity(self):
        registry=sample_registry()
        with tempfile.TemporaryDirectory() as t:
            path=Path(t)/'observations.json'
            for field in ('address','hostkey_alias','host_key_alias','ssh_alias','ssh_user'):
                path.write_text(json.dumps({'cp_0042':{field:'evil'}}))
                with self.assertRaises(online.Failure):delta_release.inventory(path,registry=registry)
            path.write_text(json.dumps({'cp_0042':{'status':'verified','last_verified_at':'old'}}))
            self.assertEqual(delta_release.inventory(path,registry=registry)['cp_0042']['status'],'verified')

    def test_ssh_identity_checked_before_construction(self):
        registry=sample_registry()
        with patch.object(online,'config_for',return_value={'user':'orangepi','hostname':'wrong'}),patch.object(online,'SSH') as ssh:
            with self.assertRaises(online.Failure):fleet.registry_ssh(online,registry,'cp_0042',Path('/synthetic/ssh'))
            ssh.assert_not_called()

    def test_ssh_uses_registry_address_and_alias(self):
        registry=sample_registry();device=next(d for d in registry.devices if d.device_id=='cp_0042')
        with patch.object(online,'config_for',return_value={'user':device.ssh_user,'hostkeyalias':device.host_key_alias}),patch.object(online,'SSH') as ssh:
            ssh.return_value.argv=['ssh','-F','/synthetic/ssh','-o','StrictHostKeyChecking=yes',device.ssh_alias]
            fleet.registry_ssh(online,registry,'cp_0042',Path('/synthetic/ssh'))
            self.assertEqual(ssh.call_args.args[1:3],(device.ssh_alias,device.address))

    def test_unknown_baseline_bypass_disabled(self):
        registry=sample_registry();raw=asdict(registry);del raw['digest']
        with patch.object(Path,'read_text',return_value=canonical_json(raw)),patch.object(online,'SSH') as ssh:
            with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
                fleet.main(['--registry','/synthetic/registry','--targets','cp_0042','--approve-unknown-baseline'])
            ssh.assert_not_called()

    def test_explicit_registry_offline_delta_no_ssh(self):
        raw=asdict(sample_registry());del raw['digest']
        with patch.object(Path,'read_text',return_value=canonical_json(raw)),patch.object(online,'SSH') as ssh:
            with contextlib.redirect_stdout(io.StringIO()):
                code=fleet.main(['--registry','/synthetic/registry','--targets','cp_0042',
                                 '--release',OVERLAY_ID,'--offline-plan'])
            self.assertEqual(code,0);ssh.assert_not_called()

    def test_target42_selected_from_registry_in_stable_offline_plan(self):
        registry=sample_registry();raw=asdict(registry);del raw['digest']
        with patch.object(Path,'read_text',return_value=canonical_json(raw)),patch.object(fleet,'build',return_value={}),patch.object(online,'SSH') as ssh:
            output=io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(fleet.main(['--registry','/synthetic/registry','--targets','cp_0042','--offline-plan']),0)
            self.assertEqual(json.loads(output.getvalue())['targets'],['cp_0042'])
            self.assertEqual(json.loads(output.getvalue())['addresses']['cp_0042'],registry.devices[-1].address)
            ssh.assert_not_called()

    def test_unknown_and_disabled_selection_zero_ssh(self):
        for registry,target in ((sample_registry(),'cp_0043'),(sample_registry(monitor=[]),'cp_0042')):
            raw=asdict(registry);del raw['digest']
            with patch.object(Path,'read_text',return_value=canonical_json(raw)),patch.object(online,'SSH') as ssh:
                with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
                    fleet.main(['--registry','/synthetic/registry','--targets',target])
                ssh.assert_not_called()

    def test_address_override_zero_ssh(self):
        raw=asdict(sample_registry());del raw['digest']
        with patch.object(Path,'read_text',return_value=canonical_json(raw)),patch.object(online,'SSH') as ssh:
            with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
                fleet.main(['--registry','/synthetic/registry','--targets','cp_0042','--address','cp_0042=192.0.2.250'])
            ssh.assert_not_called()

    def test_rehashed_outer_route_does_not_authorize_tampered_overlay(self):
        resources=dict(self.all_resources);resources['bootstrap.py']+=b'tamper'
        release,router=make_release(deepcopy(self.bom),resources,['collector'])
        self.assertTrue(self.codes(release,router,resources))

    def test_unverified_identity_observation_remains_unknown(self):
        result=compare_components(self.bom,sample_probe_result(identity_verified=False,status='identity_rejected'))
        self.assertTrue(all(c['installed']['status']=='unknown' and c['runtime']['status']=='unknown' for c in result['components']))

    def test_runtime_explicit_independent_same_file_loaded_evidence(self):
        f=next(c for c in self.bom['components'] if c['component_id']=='collector')['source']['files'][0]
        entry={'collector':{'sha256':f['sha256'],'status':'observed','source':'loaded:file:'+f['path']}}
        result=compare_components(self.bom,sample_probe_result(fields={'runtime_components':sample_evidence(entry)}))
        actual=next(c for c in result['components'] if c['component_id']=='collector')
        self.assertEqual(actual['installed']['status'],'unknown')
        self.assertEqual(actual['runtime']['evidence']['scope'],'file')
        self.assertTrue(actual['runtime']['evidence']['matches_source'])

class G1BoundaryRegression(unittest.TestCase):
    """Astra's independent counterexamples: real entry points, no real transport."""
    @classmethod
    def setUpClass(cls):
        cls.path = 'host/qc/fleet_monitor/__init__.py'
        cls.data = blob(C0, cls.path)
        source = {'schema_version':1, 'base_commit':C0,
                  'components':[record(c) for c in COMPONENT_IDS]}
        next(c for c in source['components'] if c['component_id']=='monitor')['files'] = [
            {'ref':C0,'path':cls.path,'sha256':fleet.sha(cls.data),
             'resource':cls.path,'provenance':'certified:host'}]
        cls.bom = build_bom(source_manifest=source, read_blob=blob, overlays=[])

    def check_bom(self, mutate=None):
        bom=deepcopy(self.bom)
        if mutate: mutate(bom)
        release,router=make_release(bom,{self.path:self.data},['monitor'])
        return validate_release(release=release,router=router,resources={self.path:self.data})

    def source(self, bom):
        return next(c for c in bom['components'] if c['component_id']=='monitor')['source']

    def test_b02_extra_bom_root(self):
        self.assertTrue(self.check_bom(lambda b:b.update(unexpected=True)))

    def test_b02_missing_installed_and_runtime(self):
        def mutate(b):
            for c in b['components']:
                del c['installed'];del c['runtime']
        self.assertTrue(self.check_bom(mutate))

    def test_b02_source_ref_bool(self):
        self.assertTrue(self.check_bom(lambda b:self.source(b).update(ref=True)))

    def test_b02_file_provenance_bool(self):
        self.assertTrue(self.check_bom(lambda b:self.source(b)['files'][0].update(provenance=True)))

    def test_b02_file_extra_field(self):
        self.assertTrue(self.check_bom(lambda b:self.source(b)['files'][0].update(unexpected=True)))

    def test_b02_nested_bool_extra_and_invalid_labels(self):
        mutations = [
            lambda b:self.source(b).update(sha256=True),
            lambda b:self.source(b).update(provenance=[True]),
            lambda b:self.source(b).update(provenance=['/private/absolute']),
            lambda b:self.source(b)['files'][0].update(provenance='bad\nlabel'),
            lambda b:self.source(b)['files'][0].update(provenance='/private/absolute'),
            lambda b:self.source(b)['files'][0].update(ref=True),
            lambda b:self.source(b)['files'][0].update(path='../escape'),
            lambda b:self.source(b)['files'][0].update(sha256=True),
            lambda b:self.source(b).update(unexpected=True),
            lambda b:b['components'][0].update(unexpected=True),
            lambda b:b['components'][0]['installed'].update(unexpected=True),
            lambda b:b['components'][0]['installed'].update(sha256='a'*64),
            lambda b:b['components'][0]['runtime'].update(status=True),
            lambda b:b['components'][0]['approval'].update(unexpected=True),
            lambda b:b['components'][0]['approval'].update(scope=True),
        ]
        for i, mutate in enumerate(mutations):
            with self.subTest(case=i):self.assertTrue(self.check_bom(mutate))

    def test_b02_valid_unknown_and_file_compare_positive(self):
        self.assertEqual(self.check_bom(),())
        entry={'monitor':{'sha256':fleet.sha(self.data),'status':'observed','source':'file:'+self.path}}
        observation=sample_probe_result(fields={'installed_components':sample_evidence(entry)})
        bom=compare_components(self.bom,observation)
        release,router=make_release(bom,{self.path:self.data},['monitor'])
        self.assertEqual(validate_release(release=release,router=router,resources={self.path:self.data}),())

    def test_b02_overlay_nested_shape(self):
        GovernanceRelease.setUpClass()
        mutations = [
            lambda o:o.update(unexpected=True),
            lambda o:o.update(ref=True),
            lambda o:o['resources'].update(unexpected=True),
            lambda o:o['resources'].update({'bundle.zip':True}),
            lambda o:o['approval'].update(unexpected=True),
            lambda o:o['approval'].update(expiry=True),
        ]
        for i, mutate in enumerate(mutations):
            bom=deepcopy(GovernanceRelease.bom);mutate(bom['overlays'][0])
            release,router=make_release(bom,GovernanceRelease.all_resources,['collector'])
            with self.subTest(case=i):
                self.assertTrue(validate_release(release=release,router=router,resources=GovernanceRelease.all_resources))

    def plan_main(self, overlay, mode, corruption):
        """Create a fresh plan through the real main, then consume it through main."""
        registry=sample_registry();raw=asdict(registry);del raw['digest']
        device=registry.devices[-1]
        live={'files':{},'protected':{},'camd_pid':'synthetic'}
        profile={'policy':'advisory'}
        prepared={'schema':1,'device':device.device_id,'commit':PIN,
                  'manifest_sha256':fleet.MANIFEST_SHA256,'unknown_approved':False,
                  'diff':{},'component_diff':{},'protected':{},'camd_pid':'synthetic'}
        with tempfile.TemporaryDirectory() as t,contextlib.ExitStack() as stack:
            root=Path(t);path=root/'registry.json';path.write_text(canonical_json(raw))
            output=io.StringIO();stack.enter_context(contextlib.redirect_stdout(output))
            stack.enter_context(patch.object(fleet,'registry_ssh',return_value=object()))
            stack.enter_context(patch.object(fleet,'build',return_value={'manifest_sha256':fleet.MANIFEST_SHA256}))
            stack.enter_context(patch.object(online,'read_live',return_value=live))
            stack.enter_context(patch.object(online,'prepare_plan',return_value=deepcopy(prepared)))
            stack.enter_context(patch.object(online,'matches'))
            stack.enter_context(patch.object(online,'gates'))
            classify=stack.enter_context(patch.object(delta_release,'classify',return_value='after' if mode=='verify' else 'before'))
            stack.enter_context(patch.object(delta_release,'read_profile',return_value=profile))
            delta_stage=stack.enter_context(patch.object(delta_release,'stage_apply',return_value={'status':'staged_not_applied'}))
            stage=stack.enter_context(patch.object(online,'stage_and_apply',return_value='synthetic-root'))
            components=stack.enter_context(patch.object(online,'stage_components_verify',return_value='synthetic-root'))
            verify=stack.enter_context(patch.object(online,'verify',return_value={'status':'verified','deploy_ready':True}))
            cli=['--registry',str(path),'--targets',device.device_id,'--state-dir',str(root)]
            if overlay:cli+=['--release',OVERLAY_ID]
            self.assertEqual(fleet.main(cli),0)
            created=json.loads(output.getvalue())
            plan=online.load_plan(root,(created if overlay else created['results'][0])['plan_sha256'])
            if corruption=='missing':plan.pop('registry_digest',None)
            elif corruption=='missing_identity':plan.pop('registry_device',None)
            elif corruption=='wrong':plan['registry_digest']='0'*64
            elif corruption=='identity':plan['registry_device']={'device_id':device.device_id}
            elif corruption=='config_changed':
                raw['devices'][-1]['capabilities']=list(raw['devices'][-1]['capabilities'])+['changed-context']
                path.write_text(canonical_json(raw))
            if corruption is None:
                self.assertEqual(plan.get('registry_digest'),registry.digest)
                self.assertEqual(plan.get('registry_device',{}).get('host_key_alias'),device.host_key_alias)
            digest,_=online.write_plan(root,plan)
            output.seek(0);output.truncate()
            classify.return_value='after' if mode=='verify' else 'before'
            code=fleet.main(cli+['--'+mode,'--accepted-plan',digest])
            if corruption is None:
                self.assertEqual(code,3 if overlay and mode=='apply' else 0)
                if overlay:self.assertEqual(delta_stage.call_count,1 if mode=='apply' else 0)
                elif mode=='apply':self.assertEqual(stage.call_count,1);self.assertEqual(verify.call_count,1)
                else:self.assertEqual(components.call_count,1);self.assertEqual(verify.call_count,2)
            else:
                self.assertEqual(code,2)
                for spy in (delta_stage,stage,components,verify):spy.assert_not_called()

    def test_b01_stable_real_main_create_apply_verify(self):
        for mode in ('apply','verify'):
            for corruption in (None,'missing','wrong','missing_identity','identity','config_changed'):
                with self.subTest(mode=mode,corruption=corruption):self.plan_main(False,mode,corruption)

    def test_b01_overlay_real_main_create_apply_verify(self):
        for mode in ('apply','verify'):
            for corruption in (None,'missing','wrong','missing_identity','identity','config_changed'):
                with self.subTest(mode=mode,corruption=corruption):self.plan_main(True,mode,corruption)

    def test_b01_direct_stage_boundary_cannot_bypass_registry_binding(self):
        registry=sample_registry();ssh=unittest.mock.Mock()
        with self.assertRaises((online.Failure,ValueError)):
            delta_release.stage_apply(ssh,'cp_0042',b'not-staged',b'not-staged',
                                      registry=registry,plan={})
        ssh.run.assert_not_called();ssh.require_tty.assert_not_called()
        with patch.object(online,'stage_and_apply') as stage,patch.object(online,'stage_components_verify') as components,patch.object(online,'verify') as verify:
            for plan in ({},fleet.bind_registry_plan({'device':'cp_0042','unknown_approved':True},registry=registry,device='cp_0042')):
                with self.assertRaises(ValueError):
                    fleet.stage_registry_plan(online,ssh,{},plan,{},'cp_0042',registry=registry)
                with self.assertRaises(ValueError):
                    fleet.verify_registry_components(online,ssh,plan,{},'cp_0042',registry=registry)
                with self.assertRaises(ValueError):
                    fleet.verify_registry_plan(online,{},plan,{},'cp_0042',{},registry=registry)
            for spy in (stage,components,verify):spy.assert_not_called()

    def test_b03_configuration_reread_cannot_change_final_identity(self):
        registry=sample_registry();d=registry.devices[-1]
        with patch.object(online,'config_for',side_effect=[
                {'user':d.ssh_user,'hostkeyalias':d.host_key_alias},
                {'user':'unexpected-user','hostkeyalias':'unexpected-key'}]):
            transport=fleet.registry_ssh(online,registry,d.device_id,'/synthetic/ssh')
        opts=[]
        for i,part in enumerate(transport.argv[:-1]):
            if part=='-o':opts.append(transport.argv[i+1])
        for key,value in (('hostname',d.address),('hostkeyalias',d.host_key_alias),
                          ('user',d.ssh_user),('stricthostkeychecking','yes'),('batchmode','yes')):
            values=[p.split('=',1)[1] for p in opts if p.split('=',1)[0].lower()==key]
            self.assertEqual(values,[value],key)


if __name__ == '__main__':
    unittest.main()
