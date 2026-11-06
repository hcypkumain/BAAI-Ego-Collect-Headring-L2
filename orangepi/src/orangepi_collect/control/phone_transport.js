/* Bounded reads and cancellation for live, fit-check and replay JPEG records. */
(function(global){
 'use strict';
 global.readPhoneFrames=async function(url,{headers,signal,onFrame,idleMs=8000}){
  const controller=new AbortController(),abort=()=>controller.abort();
  let rejectAbort;
  const interrupted=new Promise((_,reject)=>{rejectAbort=reject;});
  // Some mobile fetch implementations leave read()/cancel() pending after
  // abort. Race each operation against our own rejection as well.
  interrupted.catch(()=>{});
  const onAbort=()=>rejectAbort(new DOMException('Display read cancelled','AbortError'));
  controller.signal.addEventListener('abort',onAbort,{once:true});
  signal?.addEventListener('abort',abort,{once:true});if(signal?.aborted)abort();
  const guarded=value=>Promise.race([value,interrupted]);
  let reader=null,carry=null,position=0,lastFrame=performance.now(),delivered=0;
  const watchdog=setInterval(()=>{if(performance.now()-lastFrame>idleMs)controller.abort();},250);
  async function bytes(length,allowEnd=false){
   const output=new Uint8Array(length);let written=0;
   while(written<length){
    if(!carry||position>=carry.length){
     const value=await guarded(reader.read());
     if(value.done){if(allowEnd&&written===0)return null;throw Error('预览连接在一帧中途结束');}
     carry=value.value;position=0;
    }
    const count=Math.min(length-written,carry.length-position);output.set(carry.subarray(position,position+count),written);position+=count;written+=count;
   }return output;
  }
  try{
   const response=await guarded(fetch(url,{headers,signal:controller.signal,cache:'no-store'}));
   if(!response.ok){const value=await guarded(response.json()).catch(()=>({}));throw Error(value.error||value.message||'显示连接暂不可用');}
   if(!response.body?.getReader)throw Error('请使用支持连续显示的新版浏览器');reader=response.body.getReader();const decoder=new TextDecoder();
   while(!controller.signal.aborted){
    const prefix=await bytes(4,true);if(!prefix){if(!delivered)throw Error('暂未收到新画面');return;}
    const length=new DataView(prefix.buffer).getUint32(0);
    if(length<2||length>2048)throw Error('显示数据头无效');const meta=JSON.parse(decoder.decode(await bytes(length)));
    if(!Number.isInteger(meta.bytes)||meta.bytes<1||meta.bytes>2*1024*1024)throw Error('显示帧大小无效');const raw=await bytes(meta.bytes);
    await guarded(Promise.resolve(onFrame(meta,new Blob([raw],{type:'image/jpeg'}))));delivered++;lastFrame=performance.now();
   }
  }finally{
   clearInterval(watchdog);signal?.removeEventListener('abort',abort);
   // Cancellation is cleanup, never a prerequisite for reconnecting.
   if(reader){try{Promise.resolve(reader.cancel()).catch(()=>{});}catch{}try{reader.releaseLock();}catch{}}
   controller.abort();controller.signal.removeEventListener('abort',onAbort);
  }
 };
})(window);
