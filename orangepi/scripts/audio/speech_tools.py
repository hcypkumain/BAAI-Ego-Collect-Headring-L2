"""Credential-free speech cache status and bounded fixed-prompt prewarm tool."""
import argparse
import json
from pathlib import Path
import sys
import time

HERE=Path(__file__).resolve().parent
SRC=HERE.parents[1]/'src/orangepi_collect'
for directory in (SRC/'audio',SRC/'common'):
    if str(directory) not in sys.path:sys.path.insert(0,str(directory))
from speech_cloud import CloudCache
from speech_prompts import fixed_prompts

parser=argparse.ArgumentParser()
parser.add_argument('action',choices=('test','warmup'))
parser.add_argument('--text',default='语音连接测试成功。')
parser.add_argument('--force',action='store_true',help='explicitly request generation for the test phrase')
parser.add_argument('--wait-seconds',type=float,default=300)
args=parser.parse_args()
wait=max(0,min(300,args.wait_seconds))
cache=CloudCache()
try:
    if args.action=='test':
        hit=cache.lookup(args.text)
        if hit:
            print('CACHE_READY')
        elif args.force:
            generated=cache.request(args.text,wait=wait,priority='urgent',prompt_id='speech_test',force=True)
            print('GENERATED' if generated else 'NOT_READY')
        else:
            print('CACHE_MISSING; no synthesis requested')
    else:
        prompts=fixed_prompts();cache.warmup(prompts)
        deadline=time.monotonic()+wait
        while wait and cache._status_snapshot()['pending'] and time.monotonic()<deadline:time.sleep(.2)
        print(json.dumps(cache._status_snapshot(),ensure_ascii=False,separators=(',',':')))
finally:cache.close()
