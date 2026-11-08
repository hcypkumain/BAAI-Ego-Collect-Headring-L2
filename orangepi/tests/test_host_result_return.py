"""Scoped missing-directory recovery and completed-QC reuse do not relax capture guards."""
import ast,hashlib,json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

ROOT=Path(__file__).resolve().parents[2]
def function(path,name,env):
    tree=ast.parse((ROOT/path).read_text())
    node=next(x for x in tree.body if isinstance(x,ast.FunctionDef) and x.name==name)
    exec(compile(ast.Module(body=[node],type_ignores=[]),'host_return_test','exec'),env)
    return env[name]

class Tests(unittest.TestCase):
    def test_closed_result_parent_is_created_before_push(self):
        with tempfile.TemporaryDirectory() as folder:
            results=Path(folder)/'server-qc/results';finalized=Mock()
            env={'Path':Path,'RESULTS':results,'session':lambda value:Path(folder)/value,'finalized':finalized,'identity':lambda:'cp_0020'}
            prepare=function('services/headring-qc/pi_agent.py','prepare_results',env)
            value=prepare({'session_id':'TEST-ONE'})
            self.assertTrue((results/'TEST-ONE').is_dir());self.assertEqual(value['result_directory'],str(results/'TEST-ONE'))
            finalized.assert_called_once()
    def test_active_capture_refuses_directory_creation(self):
        with tempfile.TemporaryDirectory() as folder:
            results=Path(folder)/'server-qc/results'
            env={'Path':Path,'RESULTS':results,'session':lambda value:Path(folder)/value,'finalized':Mock(side_effect=RuntimeError('PI_BUSY')),'identity':lambda:'cp_0020'}
            prepare=function('services/headring-qc/pi_agent.py','prepare_results',env)
            with self.assertRaises(RuntimeError):prepare({'session_id':'TEST-ONE'})
            self.assertFalse(results.exists())
    def test_host_prepares_matching_result_path_before_rsync(self):
        text=(ROOT/'services/headring-qc/service.py').read_text()
        self.assertIn("agent(job['device'],'prepare_results'",text)
        self.assertIn("prepared.get('result_directory')!=str(destination)",text)
        self.assertIn('require_idle(job[',text)
    def test_qc_cache_reuses_only_frozen_result_and_unchanged_artifacts(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);records=root/'state';resultdir=root/'result';records.mkdir();(resultdir/'artifacts').mkdir(parents=True)
            artifact=resultdir/'artifacts/report.json';artifact.write_text('{"level":"WARN"}')
            sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
            result={'request_id':'REQ','native_sha256':'NATIVE','level':'WARN','artifacts':{'report.json':sha(artifact)}}
            (resultdir/'result.json').write_text(json.dumps(result));(records/'qc-completed.json').write_text(json.dumps({'result_sha256':sha(resultdir/'result.json')}))
            def load(path,default=None):return json.loads(path.read_text()) if path.exists() else default
            cache=function('services/headring-qc/service.py','cached_qc_result',{'load':load,'hash_file':sha,'json':json,'Path':Path})
            job={'details':json.dumps({'request_id':'REQ','native_sha256':'NATIVE'})}
            self.assertEqual(cache(job,records,resultdir)['level'],'WARN')
            artifact.write_text('tampered')
            with self.assertRaises(ValueError):cache(job,records,resultdir)
    def test_qc_cache_without_completion_proof_requires_real_qc(self):
        cache=function('services/headring-qc/service.py','cached_qc_result',{'load':lambda p:None})
        self.assertIsNone(cache({},Path('/none'),Path('/none')))
    def test_raw_transfer_and_cleanup_still_require_pi_idle(self):
        host=(ROOT/'services/headring-qc/service.py').read_text();agent=(ROOT/'services/headring-qc/pi_agent.py').read_text()
        self.assertIn('require_idle(did)',host);self.assertIn('idle();path=root/identifier',agent)
        self.assertIn('source_manifest(path,progress=idle_progress())',agent)

if __name__=='__main__':unittest.main(verbosity=2)
