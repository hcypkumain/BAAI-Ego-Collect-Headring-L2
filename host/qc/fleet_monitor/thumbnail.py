"""Executed temporarily on an enrolled headring; reads only an allowed diagram."""
import sys,json,urllib.request,hashlib,io,base64
from pathlib import Path

device=Path('/opt/egorecord/DEVICE_ID').read_text().strip()
if device!=sys.argv[1]:raise RuntimeError('HEADRING_IDENTITY_MISMATCH')
expected=sys.argv[2];gate_only=len(sys.argv)>3 and sys.argv[3]=='gate'
base=next(p for p in (Path('/home/orangepi/Desktop/Embodied-Brain-Collect-main.2/orangepi'),Path('/home/orangepi/Desktop/Embodied-Brain-Collect-main/orangepi')) if (p/'runtime/dashboard_token').is_file())
token=(base/'runtime/dashboard_token').read_text().strip()
def read(endpoint,binary=False):
    request=urllib.request.Request('http://127.0.0.1:8765/api/'+endpoint,headers={'X-Access-Token':token})
    with urllib.request.urlopen(request,timeout=4) as r:
        value=r.read(16*1024*1024+1)
        if len(value)>16*1024*1024:raise ValueError('图纸过大')
        return value if binary else json.loads(value)
def authorize():
    gate=read('display-state');full=read('status?signals=&review=0&cameras=0&display=fleet-image-readonly')
    privacy=gate.get('privacy') or {}
    if privacy.get('required') is not False or privacy.get('stop_confirmed') is not True or (gate.get('worker') or {}).get('recording_active') is True:raise PermissionError('当前采集阶段不显示图纸')
    key=[gate.get('collection_context'),gate.get('image'),full.get('image_name')]
    actual=hashlib.sha256(json.dumps(key,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    if actual!=expected:raise ValueError('图纸已变化，请刷新后查看')
    return gate
try:
    before=authorize()
    if gate_only:print(json.dumps({'ok':True,'device':device,'asset':expected}))
    else:
        from PIL import Image
        raw=read('reference',True)
        with Image.open(io.BytesIO(raw)) as image:
            if image.width*image.height>40000000:raise ValueError('图纸尺寸过大')
            image.thumbnail((480,480));buffer=io.BytesIO();image.convert('RGB').save(buffer,format='WEBP',quality=75)
        authorize();data=buffer.getvalue()
        if len(data)>160000:raise ValueError('缩略图超过传输预算')
        print(json.dumps({'ok':True,'device':device,'asset':expected,'data':base64.b64encode(data).decode()}))
except Exception as e:print(json.dumps({'ok':False,'message':str(e)[:180]}))
