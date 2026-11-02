"""Same-origin authenticated phone routes; responses never contain secrets."""
import json
from urllib.parse import parse_qs,urlsplit
from cloud_common import CloudError

def handle_cloud(handler,owner,method):
    url=urlsplit(handler.path)
    if not url.path.startswith('/api/phone/cloud/'):return False
    manager=getattr(owner,'cloud_uploads',None)
    if manager is None:handler.send(503,{'error':'上传服务暂不可用，请检查服务状态'});return True
    try:
        path=url.path[len('/api/phone/cloud/'):]
        if method=='GET' and path=='status':
            query=parse_qs(url.query,keep_blank_values=True)
            if set(query)-{'day'} or any(len(v)!=1 for v in query.values()):raise CloudError('上传列表请求无效')
            handler.send(200,manager.status(query.get('day',['today'])[0]));return True
        if method!='POST' or url.query:raise CloudError('上传操作无效')
        if handler.headers.get('Content-Type','').split(';')[0].strip()!='application/json':raise CloudError('上传请求必须为 JSON')
        length=int(handler.headers.get('Content-Length','0'))
        if not 1<=length<=65536:raise CloudError('上传请求大小无效')
        raw=handler.rfile.read(length);value=json.loads(raw)
        if not isinstance(value,dict):raise CloudError('上传操作格式无效')
        if path=='credentials':result=manager.set_credentials(value)
        elif path=='clear-credentials' and not value:result=manager.clear_credentials()
        elif path=='queue':result=manager.enqueue(value)
        elif path=='retry' and set(value)=={'job_id'}:result=manager.retry(value['job_id'])
        elif path=='control' and set(value)=={'action'}:result=manager.control(value['action'])
        elif path=='cleanup':result=manager.cleanup(value)
        else:raise CloudError('未知上传操作')
        handler.send(200,result)
    except CloudError as error:handler.send(409,{'error':str(error)})
    except (OSError,ValueError,TypeError,KeyError):handler.send(400,{'error':'上传操作未确认，请刷新后重试；本地数据保留'})
    return True
