"""Generate distributable safety audio with Aliyun only. Requires valid credentials/network."""
import sys,json,hashlib,shutil,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'src/orangepi_collect/common'))
from project_paths import BASE
from speech_cloud import CloudCache
TEXTS={'wait':'请等待，正在获取语音。','fatal':'语音服务不可用，程序已安全停止。请检查网络或密钥后重新启动。',
       'api_error':'阿里云语音接口返回错误，错误码：','timeout':'语音请求等待超时。','unknown':'错误码无法完整播报，请查看语音状态日志。'}
TEXTS.update({'char_'+str(i):word for i,word in enumerate('零一二三四五六七八九')})
TEXTS.update({'char_'+c:'字母 '+c.upper() for c in 'abcdefghijklmnopqrstuvwxyz'})
TEXTS.update(char_underscore='下划线',char_dash='连接号',char_dot='点')
if __name__=='__main__':
    root=BASE/'assets/cloud_voice';root.mkdir(parents=True,exist_ok=True);cloud=CloudCache();manifest={}
    try:
        for name,text in TEXTS.items():
            path=cloud.request(text,wait=95)
            if path is None:raise RuntimeError('Cloud safety prompt unavailable: '+name)
            target=root/(name+'.wav');shutil.copyfile(path,target)
            manifest[name]={'text':text,'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'source':'Aliyun DashScope','model':cloud.config['model'],'voice':cloud.config['voice']}
            print('READY '+name,flush=True)
        (root/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    finally:cloud.close()
