import ast,types,unittest
from pathlib import Path
root=Path(__file__).resolve().parents[1]/'src/orangepi_collect/control';f=root/'reference_controls.py';tree=ast.parse(f.read_text());func=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='refresh_reference_histories');code=compile(ast.fix_missing_locations(ast.Module(body=[func],type_ignores=[])),'candidate','exec');ns={'Path':Path};exec(code,ns)
class Ledger:
 def __init__(self):self.images_calls=0;self.read_calls=0;self.valid_calls=0;self.reserved_calls=0;self.doc={'used':{'one.png':{'status':'success'},'two.png':{'status':'pending_qc'},'bad.png':{'status':'pending_qc','broken':True}},'attempts':{'bad.png':[{'has_data':True,'qc_level':'ERROR'}]}}
 def images(self):self.images_calls+=1;return [Path('/project/task')/x for x in ('one.png','two.png','bad.png','new.png')]
 def read(self,p):self.read_calls+=1;return self.doc
 def valid_used(self,e,image):self.valid_calls+=1;return e.get('status')=='success'
 def reserved_used(self,e,image):self.reserved_calls+=1;return e.get('status')=='pending_qc' and not e.get('broken')
class T(unittest.TestCase):
 def test_complete_reserved_and_history_remain_distinct(self):
  l=Ledger();o=types.SimpleNamespace(_reference_ledger=l,_reference_project=Path('/project'));done,res=ns['refresh_reference_histories'](o);self.assertEqual(done,('task/one.png',));self.assertEqual(res,('task/two.png',));self.assertEqual(o._reference_histories['task/bad.png']['state'],'attempted_uncollected');self.assertEqual(o._reference_histories['task/new.png']['state'],'unattempted');self.assertEqual((l.images_calls,l.read_calls,l.valid_calls,l.reserved_calls),(1,1,3,3))
 def test_reaped_still_unpins_and_updates_outcome_when_refresh_deferred(self):
  tree=ast.parse((root/'reference_controls.py').read_text());method=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='reference_reaped');scope={};exec(compile(ast.fix_missing_locations(ast.Module(body=[method],type_ignores=[])),'candidate','exec'),scope)
  calls=[];blocks=types.SimpleNamespace(success=lambda entry:calls.append(('success',entry)),defer=lambda entry:calls.append(('defer',entry)))
  o=types.SimpleNamespace(_reference_active={'entry':'item'},_reference_reap_outcome='pending_qc',_reference_task_blocks=blocks,_reference_mutation_started=lambda:calls.append('mutation'),reference_release_pin=lambda:calls.append('unpin'),reference_refresh=lambda **kw:calls.append('refresh'))
  scope['reference_reaped'](o,refresh=False);self.assertEqual(calls,['mutation',('defer','item'),'unpin']);self.assertIsNone(o._reference_active)
  calls.clear();o._reference_active=None;o._reference_reap_outcome=None;scope['reference_reaped'](o);self.assertEqual(calls,['mutation','unpin','refresh'])
 def test_early_refresh_deferred_only_abandon(self):
  s=(root/'web_controls.py').read_text();start=s.index('    def abandon_preparation(');end=s.index('    def delete_recording(',start);block=s[start:end];self.assertIn('self.reap(refresh=False)',block);self.assertIn('self.reference_refresh(force=True)',block);self.assertLess(block.index('self.reap(refresh=False)'),block.index('self.reference_refresh(force=True)'))
unittest.main()
