import sys,types,datetime,threading,tempfile,time
from pathlib import Path
p=Path(__file__).resolve().parents[1]/'src/orangepi_collect/control';sys.path.insert(0,str(p))
r=types.ModuleType('recording_store');r.CST=datetime.timezone(datetime.timedelta(hours=8));r.bounded=lambda p,n:Path(p).read_text();r.atomic_json=lambda p,v: None;sys.modules['recording_store']=r
q=types.ModuleType('qc_details');q.summarize_qc=lambda _:dict(error_count=0,warn_count=0,findings=[]);sys.modules['qc_details']=q
c=types.ModuleType('cloud_common');c.sync_dir=lambda p:None;sys.modules['cloud_common']=c
from daily_collection import DailyCollection
with tempfile.TemporaryDirectory() as tmp:
 root=Path(tmp); (root/'ops').mkdir(); (root/'data').mkdir(); entered=threading.Event();release=threading.Event(); state={'state':'idle'}
 class Store:
  operations=root/'ops';project=None;calls=0
  def roots(self):
   self.calls+=1
   if self.calls==1:
    entered.set();assert release.wait(3)
   return None,root
 store=Store();d=DailyCollection(root,store,lambda:state,autostart=False)
 result=[]
 background=threading.Thread(target=lambda: result.append(d.refresh()));background.start();assert entered.wait(1)
 exported=[]
 thread=threading.Thread(target=lambda:exported.append(d.export()));thread.start();time.sleep(.05);assert thread.is_alive();release.set();background.join(2);thread.join(2)
 assert result==[True] and len(exported)==1 and exported[0][0].is_file();assert store.calls==3,store.calls
 # When the joined refresh was interrupted by an unsafe state, no stale report is generated.
 state['state']='recording';
 try:d.export();raise AssertionError('unsafe export accepted')
 except ValueError as e:assert '结束采集' in str(e)
 # A refresh interrupted by an unsafe transition must not make a report.
 state['state']='idle'; entered.clear();release.clear();store.calls=0
 bg=threading.Thread(target=d.refresh);bg.start();assert entered.wait(1)
 failure=[]
 def do_export():
  try:d.export()
  except ValueError as e:failure.append(str(e))
 exp=threading.Thread(target=do_export);exp.start();time.sleep(.05);state['state']='recording';release.set();bg.join(2);exp.join(2)
 assert len(failure)==1 and '每日统计正在更新' in failure[0]
 class TimeoutLock:
  def acquire(self,timeout):assert timeout==20;return False
 d.refresh_lock=TimeoutLock();state['state']='idle';assert d.refresh(wait=True) is False
 print('Concurrent refresh join, interrupted refresh, bounded wait and unsafe state: passed')
