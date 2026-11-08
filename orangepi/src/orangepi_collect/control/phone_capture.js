/* Camera fit acknowledgement and local export; capture remains owned by Pi. */
(function(global){
 'use strict';
 global.createCameraFitCheck=function(options){
  const root=options.root;root.innerHTML='<div class="phone-fit-toolbar"><button id="phone-fit-back">返回参考图与采集准备</button><button id="phone-fit-abandon">放弃本次采集</button><button id="phone-fit-export" disabled>倒计时拍照（5秒）</button><a id="phone-fit-save" hidden>保存刚拍的照片</a></div><p id="phone-fit-note" role="status">正在读取全部相机…</p><div id="phone-fit-stage"><canvas id="phone-fit-image" hidden role="img" aria-label="全部启用相机的同屏检查图"></canvas><div id="phone-photo-countdown" hidden><b id="phone-photo-number">5</b><span>请平伸手掌，倒计时结束后拍照</span><button id="phone-photo-cancel">取消拍照</button></div></div><button id="phone-fit-start" class="primary" disabled>确认画面方向，返回准备页</button>';
  const $=id=>document.getElementById(id);let snapshot=null,version=0,controller=null,timer=null,blob=null,metadata=null,context='',starting=false,shot=null,shotVersion=0,shotURL='',receivedAt=0;
  function active(){return options.active()&&!document.hidden&&options.unlocked();}
  function stop(){cancelShot();if(shotURL){URL.revokeObjectURL(shotURL);shotURL='';}$('phone-fit-save').hidden=true;version++;clearTimeout(timer);timer=null;controller?.abort();controller=null;metadata=blob=null;const canvas=$('phone-fit-image');canvas.hidden=true;canvas.width=canvas.height=0;$('phone-fit-export').disabled=true;$('phone-fit-start').disabled=true;}
  function controls(){$('phone-fit-abandon').hidden=!snapshot?.actions?.includes('abandon');$('phone-fit-abandon').disabled=starting;const eligible=snapshot?.camera_check?.eligible===true;$('phone-fit-start').disabled=!active()||starting||!!shot||!eligible||!metadata?.complete;$('phone-fit-export').disabled=!active()||!blob||starting||!!shot;}
  function schedule(delay=0){clearTimeout(timer);timer=null;if(active())timer=setTimeout(()=>{timer=null;load();},delay);}
  async function showFrame(identity,request,nextMeta,nextBlob){
   if(identity!==version||!active()||request.signal.aborted){request.abort();return;}
   const bitmap=await createImageBitmap(nextBlob);
   try{
    if(identity!==version||!active()||request.signal.aborted)return;
    const canvas=$('phone-fit-image');canvas.width=bitmap.width;canvas.height=bitmap.height;canvas.getContext('2d').drawImage(bitmap,0,0);
   }finally{bitmap.close();}
   const canvas=$('phone-fit-image');
   blob=nextBlob;metadata=nextMeta;canvas.hidden=false;
   $('phone-fit-note').textContent=metadata.camera_count+' 路相机 · 请检查头环佩戴是否端正、腕部相机朝向是否正确。'+(!metadata.complete?'有相机缺少新画面，暂不可确认。':snapshot?.camera_check?.eligible?'确认只完成检查；返回准备页后可继续看参考图，再独立开始正式采集。':'可预览和导出；请先完成设备准备与阻抗确认。');controls();
  }
  async function load(){
   if(!active()||controller)return;const identity=version,started=performance.now(),request=new AbortController();controller=request;
   const timeout=setTimeout(()=>request.abort(),2200);
   try{
     const columns=innerHeight>innerWidth?2:3;
     // Short snapshot requests avoid buffered-stream/slot exhaustion while
     // preserving the server's exact fresh-frame acknowledgement proof.
     const response=await fetch('/api/phone/camera-check.jpg?columns='+columns,{headers:options.headers(),signal:request.signal,cache:'no-store'});
     if(!response.ok)throw Error('相机快照暂不可用，请重试');
     const meta=JSON.parse(response.headers.get('X-Camera-Check-Meta')||'{}');
     if(typeof meta.check_id!=='string'||!/^cc_[0-9a-f]{32}$/.test(meta.check_id)||typeof meta.complete!=='boolean')throw Error('相机检查证明无效');
     const nextBlob=await response.blob();
     await showFrame(identity,request,meta,nextBlob);
    }catch(error){if(identity===version&&active()){metadata=null;controls();$('phone-fit-note').textContent=error.name==='AbortError'?'读取超时，正在重新同步…':error.message;}}
   finally{clearTimeout(timeout);if(controller===request)controller=null;if(identity===version)schedule(Math.max(150,600-(performance.now()-started)));}
  }
  $('phone-fit-abandon').onclick=()=>options.action('abandon');
  $('phone-fit-back').onclick=()=>options.back();
  function cancelShot(){shotVersion++;if(shot){clearTimeout(shot.timer);shot.request?.abort();shot=null;}$('phone-photo-countdown').hidden=true;controls();}
  async function captureAfterCountdown(identity){
   if(!shot||identity!==shotVersion||!active())return;
   $('phone-photo-number').textContent='拍照中';const deadline=performance.now()+4000;
   try{
    while(shot&&identity===shotVersion&&active()){
     const request=new AbortController();shot.request=request;const timeout=setTimeout(()=>request.abort(),1800);
     let response;
     try{response=await fetch('/api/phone/camera-check.jpg?columns='+(innerHeight>innerWidth?2:3)+'&after_unix_ms='+Math.ceil(shot.after)+'&request_id='+shot.request_id,{headers:options.headers(),signal:request.signal,cache:'no-store'});}finally{clearTimeout(timeout);}
     if([425,429].includes(response.status)&&performance.now()<deadline){await new Promise(resolve=>setTimeout(resolve,150));continue;}
     if(!response.ok){const error=await response.json().catch(()=>({}));throw Error(error.error||'未取得倒计时结束后的新画面，请重试。');}
     const next=await response.blob(),meta=JSON.parse(response.headers.get('X-Camera-Check-Meta')||'{}');
     if(!shot||identity!==shotVersion||!active())return;if(!meta.complete||meta.captured_at_ms<shot.after)throw Error('拍照结果不是倒计时后的完整新画面，请重试。');
     if(shotURL)URL.revokeObjectURL(shotURL);shotURL=URL.createObjectURL(next);
     const save=$('phone-fit-save');save.href=shotURL;save.download=meta.filename||'camera-check.jpg';save.hidden=false;save.click();
     $('phone-fit-note').textContent='已在倒计时结束后取得新画面。若手机没有自动保存，请点“保存刚拍的照片”。';
     cancelShot();return;
    }
   }catch(error){if(identity===shotVersion&&active()){$('phone-fit-note').textContent=error.name==='AbortError'?'拍照读取超时，请重试。':error.message;cancelShot();}}
  }
  $('phone-photo-cancel').onclick=()=>{cancelShot();$('phone-fit-note').textContent='已取消拍照，未导出图片。';};
  $('phone-fit-export').onclick=()=>{
   if(!blob||!metadata||!active()||shot)return;const now=performance.now(),server=(snapshot?.server_time||Date.now()/1000)*1000+(now-receivedAt);
   const identity=++shotVersion;shot={deadline:now+5000,after:server+5000,timer:null,request:null,request_id:'photo_'+Date.now().toString(36)+'_'+Math.random().toString(36).slice(2)};$('phone-photo-countdown').hidden=false;controls();
   function tick(){if(!shot||identity!==shotVersion||!active()){cancelShot();return;}const left=Math.ceil((shot.deadline-performance.now())/1000);if(left<=0){captureAfterCountdown(identity);return;}$('phone-photo-number').textContent=String(left);shot.timer=setTimeout(tick,100);}
   tick();
  };
  $('phone-fit-start').onclick=async()=>{
   if(starting||!metadata?.complete||!snapshot?.camera_check?.eligible||!active())return;starting=true;controls();
   try{
    const response=await fetch('/api/phone/camera-check/confirm',{method:'POST',headers:{'Content-Type':'application/json',...options.headers()},body:JSON.stringify({check_id:metadata.check_id})});
    const value=await response.json();if(!response.ok)throw Error(value.error||'相机检查确认失败');
    await options.refresh();options.confirmed();
   }catch(error){$('phone-fit-note').textContent=error.message;}
   finally{starting=false;controls();}
  };
  return {open(){stop();schedule();},suspend(){stop();},update(value){snapshot=value;receivedAt=performance.now();const key=value.camera_check?.context||'';if(context&&context!==key)stop();context=key;controls();if(active()&&!controller&&timer===null)schedule();else if(!active()&&(controller||metadata||blob))stop();}};
 };
})(window);
