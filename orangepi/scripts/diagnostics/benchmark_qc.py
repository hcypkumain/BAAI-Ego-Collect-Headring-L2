"""Read-only comparison against an existing QC report, with production CPU limits."""
import argparse,json,os,time
from pathlib import Path
os.environ['OPENBLAS_NUM_THREADS']='1'
os.environ['OMP_NUM_THREADS']='1'
os.environ['OPENCV_FFMPEG_CAPTURE_OPTIONS']='threads;1'
os.sched_setaffinity(0,{max(os.sched_getaffinity(0))});os.nice(5)
import resource
resource.setrlimit(resource.RLIMIT_AS,(1536*1024*1024,1536*1024*1024))
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'src/orangepi_collect/common'))
import project_paths
import framework_qc as q
import cv2
cv2.setNumThreads(1)
p=argparse.ArgumentParser();p.add_argument('session',type=Path);p.add_argument('--output',type=Path,required=True);a=p.parse_args();a.session=a.session.resolve()
t=time.monotonic();r=q.parallel_qc_session(a.session,q.load_checker(),workers=1);elapsed=time.monotonic()-t
original=a.session/'qc_report.json'
old=json.loads(original.read_text()) if original.exists() else None;new=r.to_dict()
q.atomic_json(a.output.with_suffix('.report.json'),new)
result={'seconds':elapsed,'same_report':old==new if old is not None else None,'level':r.level,'affinity':sorted(os.sched_getaffinity(0)),'source':str(a.session),'workers':1}
q.atomic_json(a.output,result);print(result,flush=True)
