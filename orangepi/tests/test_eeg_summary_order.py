import ast,pathlib,unittest
p=pathlib.Path(__file__).resolve().parents[1]/'src/orangepi_collect/framework/framework_recorder.py';tree=ast.parse(p.read_text());node=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and any(isinstance(x,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='summary' for t in x.targets) and isinstance(x.value,ast.Call) and isinstance(x.value.func,ast.Name) and x.value.func.id=='standardize' for x in ast.walk(n)))
body=next(body for n in ast.walk(node) for body in [getattr(n,'body',[])] if isinstance(body,list) and any(isinstance(x,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='summary' for t in x.targets) and isinstance(x.value,ast.Call) and isinstance(x.value.func,ast.Name) and x.value.func.id=='standardize' for x in body));start=next(i for i,x in enumerate(body) if isinstance(x,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='summary' for t in x.targets) and isinstance(x.value,ast.Call) and isinstance(x.value.func,ast.Name) and x.value.func.id=='standardize');end=next(i for i in range(start,len(body)) if isinstance(body[i],ast.Assign) and any(isinstance(t,ast.Name) and t.id=='report' for t in body[i].targets));block=compile(ast.fix_missing_locations(ast.Module(body=body[start:end+1],type_ignores=[])),'candidate','exec')
class Clock:
 def monotonic(self):return 1
 def time(self):return 1
class Test(unittest.TestCase):
 def runblock(self,fail=False):
  disk={'samples':17950};seq=[]
  def write(path,data):
   if str(path).endswith('capture_summary.json'):
    if fail:raise OSError('disk failure')
    disk.update(data['eeg']);seq.append('summary')
  def check(path,progress):seq.append('check');return disk['samples']
  ns=dict(standardize=lambda p:{'eeg':{'samples':17925}},path=pathlib.Path('/tmp/fake'),time=Clock(),qc_started=0,processing_started=0,atomic_json=write,check_session=check,os=__import__('os'))
  try:exec(block,ns)
  except OSError:pass
  return seq,ns
 def test_updated_summary_before_check(self):
  seq,ns=self.runblock();self.assertEqual(seq,['summary','check']);self.assertEqual(ns['report'],17925)
 def test_failed_write_stops_qc(self):
  seq,_=self.runblock(True);self.assertEqual(seq,[])
unittest.main()
