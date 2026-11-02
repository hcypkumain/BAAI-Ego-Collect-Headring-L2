"""Credential-free diagnostics/prewarming; never prints credentials or SDK errors."""
import argparse,ast,json,time
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'src/orangepi_collect/common'))
from project_paths import BASE, SRC
from speech_cloud import CloudCache
p=argparse.ArgumentParser();p.add_argument('action',choices=['test','warmup']);a=p.parse_args()
cloud=CloudCache()
try:
    if a.action=='test':
        out=cloud.request('语音连接测试成功。测试编号，'+time.strftime('%H%M%S')+'。',wait=95)
        print('CLOUD_AUDIO_READY' if out else 'CLOUD_UNAVAILABLE')
        status=BASE/'runtime/speech_status.json'
        if status.exists():print(status.read_text())
    else:
        text=['一','二','三']
        for node in ast.walk(ast.parse((SRC/'control/launcher.py').read_text())):
            if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr=='say' and node.args and isinstance(node.args[0],ast.Constant) and isinstance(node.args[0].value,str):text.append(node.args[0].value)
        import importlib.util
        spec=importlib.util.spec_from_file_location('guides',BASE.parent/'src/embodied_brain_collect/session/troubleshooting.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        text.extend(m.SLOT_GUIDES.values());text=list(dict.fromkeys(text))
        for item in text:cloud.request(item)
        deadline=time.monotonic()+300
        while cloud.pending and time.monotonic()<deadline:time.sleep(.5)
        print(json.dumps({'cached':sum(cloud.path(t).exists() for t in text),'total':len(text)}))
finally:cloud.close()
