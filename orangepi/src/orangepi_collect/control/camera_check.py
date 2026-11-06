"""Latest-frame contact sheet and operator acknowledgement before capture."""
import copy,hashlib,json,math,re,secrets,threading,time
from io import BytesIO
from pathlib import Path

NAME=re.compile(r'^cam[a-z0-9_]{0,60}$')
LABELS={'cam_ros_left':'Headband front left','cam_ros_right':'Headband front right',
        'cam_ros_bleft':'Headband rear left','cam_ros_bright':'Headband rear right',
        'cam_left_wrist':'Left wrist','cam_right_wrist':'Right wrist','cam_third':'Third-person'}

class CameraCheckError(ValueError):
    def __init__(self,message,status=409):super().__init__(message);self.status=status

class CameraChecks:
    def __init__(self,owner):
        self.owner=owner;self.lock=threading.Lock();self.slot=threading.BoundedSemaphore(1)
        self.records={};self.confirmed=None;self.cached=None;self.photo_requests={}

    def context(self):
        with self.owner.lock:
            s=copy.deepcopy({k:self.owner.snapshot.get(k) for k in ('state','session','selected_devices','devices','worker','eeg_impedance','wrist_calibration')})
        selected=s.get('selected_devices');devices=s.get('devices') or []
        cameras=[d for d in devices if NAME.fullmatch(str(d.get('name',''))) and (d['name'] in selected if selected is not None else d.get('connected'))]
        order=list(LABELS)
        cameras.sort(key=lambda d:order.index(d['name']) if d['name'] in order else len(order))
        if len(cameras)>12:raise CameraCheckError('启用的相机数量超过检查页上限')
        imp=s.get('eeg_impedance') or {};worker=s.get('worker') or {}
        raw=[s.get('session'),worker.get('pid'),[(d['name'],d.get('config')) for d in cameras],
             imp.get('measurement_generation'),imp.get('reviewed_at'),imp.get('completed_at'),s.get('wrist_calibration')]
        key=hashlib.sha256(json.dumps(raw,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        p=self.owner.privacy.snapshot()
        visible=p.get('phase')=='idle' and p.get('stop_confirmed') is True and not worker.get('recording_active')
        eligible=visible and s.get('state')=='ready' and worker.get('state')=='ready'
        if eligible and imp.get('required'):
            from dashboard import impedance_allows_recording
            eligible=impedance_allows_recording(imp)
        return key,s,cameras,visible,eligible

    def status(self):
        key,s,cameras,visible,eligible=self.context()
        with self.lock:confirmed=bool(self.confirmed and self.confirmed[0]==key)
        return dict(required=bool(cameras),eligible=eligible,confirmed=confirmed,camera_count=len(cameras),context=key[:24])

    def approved(self):
        value=self.status()
        if not value['eligible'] or value['required'] and not value['confirmed']:return False
        _,_,cameras,_,_=self.context()
        try:return all(0<=time.time()-(self.owner.preview/(camera['name']+'.jpg')).stat().st_mtime<3 for camera in cameras)
        except OSError:return False

    def confirm(self,identifier):
        key,s,cameras,visible,eligible=self.context()
        if not eligible:raise CameraCheckError('请先完成设备准备')
        with self.lock:
            record=self.records.get(identifier)
            if (not record or record['context']!=key or time.monotonic()-record['created']>10
                    or not record['complete'] or any(time.time()-t>3 for t in record['times'])):
                raise CameraCheckError('相机画面已过期或不完整，请确认当前画面后重试')
            self.confirmed=(key,time.monotonic())
        return dict(confirmed=True,camera_count=len(cameras))

    def sheet(self,columns=3,after_unix_ms=None,request_id=None):
        if columns not in (2,3):raise CameraCheckError('无效的检查图布局',400)
        requested_columns=columns
        key,s,cameras,visible,eligible=self.context()
        if not visible or s.get('state') in ('preparing','finalizing','countdown','recording'):
            raise CameraCheckError('采集优先，当前步骤不显示相机画面',423)
        if not cameras:raise CameraCheckError('本次未启用相机',404)
        if request_id is not None:
            if after_unix_ms is None or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',request_id):raise CameraCheckError('拍照请求无效',400)
            with self.lock:
                now=time.monotonic();self.photo_requests={k:v for k,v in self.photo_requests.items() if now-v['created']<10}
                prior=self.photo_requests.get(request_id)
                if prior is None:
                    while len(self.photo_requests)>=32:self.photo_requests.pop(next(iter(self.photo_requests)))
                    prior=dict(context=key,created=now,after=max(after_unix_ms,time.time()*1000));self.photo_requests[request_id]=prior
                if prior['context']!=key:raise CameraCheckError('拍照上下文已变化，请重新倒计时',409)
                after_unix_ms=prior['after']
        with self.lock:
            if after_unix_ms is None and self.cached and self.cached[0]==(key,columns) and time.monotonic()-self.cached[1]<.3:return self.cached[2:]
        if not self.slot.acquire(False):raise CameraCheckError('检查图正在更新，请稍候',429)
        try:
            from PIL import Image,ImageDraw,ImageFont
            resampling=getattr(Image,'Resampling',Image)
            width,height,bar=640,400,32;columns=min(columns,len(cameras));rows=math.ceil(len(cameras)/columns)
            sheet=Image.new('RGB',(columns*width,rows*(height+bar)+40),'#101820');draw=ImageDraw.Draw(sheet)
            try:font=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',20)
            except OSError:font=ImageFont.load_default()
            draw.text((10,8),time.strftime('%Y-%m-%d %H:%M:%S',time.localtime())+' | Camera fit check | '+str(len(cameras))+' cameras',fill='white',font=font)
            complete=True;times=[]
            for index,camera in enumerate(cameras):
                name=camera['name'];x=index%columns*width;y=index//columns*(height+bar)+40
                label=LABELS.get(name,name);path=self.owner.preview/(name+'.jpg')
                if name in ('cam_left_wrist','cam_right_wrist') and not (s.get('wrist_calibration') or {}).get('valid'):
                    label='Wrist source '+('A' if name=='cam_left_wrist' else 'B')+' | role unconfirmed'
                try:
                    info=path.stat()
                    if path.is_symlink() or not 0<info.st_size<=2*1024*1024 or not 0<=time.time()-info.st_mtime<3:raise OSError()
                    if after_unix_ms is not None and info.st_mtime*1000<after_unix_ms:raise CameraCheckError('正在等待倒计时结束后的新画面',425)
                    with Image.open(BytesIO(path.read_bytes())) as image:
                        if image.format!='JPEG' or max(image.size)>4096 or image.width*image.height>8*1024*1024:raise OSError()
                        natural=image.size;image.draft('RGB',(width,height));image.thumbnail((width,height),resampling.BILINEAR)
                        sheet.paste(image.convert('RGB'),(x+(width-image.width)//2,y+bar+(height-image.height)//2))
                    times.append(info.st_mtime);caption=label+' | '+str(natural[0])+'x'+str(natural[1])
                except CameraCheckError:raise
                except (OSError,ValueError):
                    complete=False;caption=label+' | NO FRESH FRAME';draw.text((x+30,y+bar+height//2),'Waiting for camera',fill='#ffb4a9',font=font)
                draw.rectangle((x,y,x+width-1,y+height+bar-1),outline='#5f7783',width=2)
                draw.rectangle((x+1,y+1,x+width-2,y+bar-1),fill='#243641');draw.text((x+10,y+5),caption,fill='white',font=font)
            fresh,_,_,still_visible,_=self.context()
            if not still_visible or fresh!=key:raise CameraCheckError('采集状态已变化，检查图已丢弃',423)
            if after_unix_ms is not None and not complete:raise CameraCheckError('未取得全部相机的新画面，请检查连接后重试',425)
            output=BytesIO();sheet.save(output,'JPEG',quality=90);raw=output.getvalue()
            identifier='cc_'+secrets.token_hex(16)
            metadata=dict(check_id=identifier,complete=complete,camera_count=len(cameras),eligible=eligible,
                          captured_at_ms=round(time.time()*1000),frame_preview_mtime_ms=[round(value*1000) for value in times],
                          photo_not_before_unix_ms=after_unix_ms,
                          filename='camera-check-'+time.strftime('%Y%m%d-%H%M%S')+'.jpg')
            with self.lock:
                self.records[identifier]=dict(context=key,created=time.monotonic(),complete=complete,times=times)
                while len(self.records)>12:self.records.pop(next(iter(self.records)))
                self.cached=((key,requested_columns),time.monotonic(),raw,metadata)
            return raw,metadata
        finally:self.slot.release()
