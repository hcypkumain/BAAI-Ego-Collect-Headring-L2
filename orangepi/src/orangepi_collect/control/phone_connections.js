/* Local proxy transport is explicit; a localhost origin is never a Pi address. */
(function(global){
 'use strict';
 global.createPhoneConnections=function(options){
  const root=options.root,status=document.createElement('p'),paths=document.createElement('p'),detail=document.createElement('p');
  status.id='phone-link-status';paths.id='phone-link-addresses';paths.className=detail.className='phone-hint';paths.style.whiteSpace='pre-wrap';root.append(status,paths,detail);
  const proxy=/^(?:localhost|127\.0\.0\.1|cp-[0-9]{4}\.localhost)$/.test(location.hostname);
  let latest=null,transport=null,pending=false;
  function paint(){
   const usb=proxy&&transport?.transport==='USB'&&transport.listener_ready===true&&transport.connected===true;
   const kind=usb?'USB':proxy?'本机入口 · 传输待确认':'局域网 · 网络类型待确认';
   status.textContent=kind+' · 页面入口 '+location.host;options.onKind?.(kind);options.onEndpoint?.(usb?transport:null);
   const interfaces=latest?.interfaces;
   const label={wireless:'Wi-Fi',usb:'USB',other:'其他网络'};
   const entries=Array.isArray(interfaces)?interfaces.map(v=>(label[v.kind]||'其他网络')+' · '+v.interface+' · '+v.family+'：'+v.address):[];
   const usbKnown=usb&&!entries.some(v=>v.endsWith('：'+transport.device_address));
   paths.textContent='当前头环 IP 地址\n'+(usbKnown?'USB 连接地址：'+transport.device_address+':8765\n':'')+(entries.length?entries.join('\n'):Array.isArray(interfaces)?'暂无活动网络地址':'当前头环尚未提供完整接口地址');
   detail.textContent=usb?'正在通过手机本机代理和 USB 网络共享连接头环。':'连接类型需由实际传输证据确认，不根据页面地址推断为 Wi-Fi。';
  }
  async function json(url){
   const controller=new AbortController();let reject;const cancelled=new Promise((_,no)=>{reject=no;});cancelled.catch(()=>{});
   const timer=setTimeout(()=>{controller.abort();reject(new DOMException('连接信息读取超时','TimeoutError'));},2500);
   try{const response=await Promise.race([fetch(url,{headers:options.headers(),cache:'no-store',signal:controller.signal}),cancelled]);if(!response.ok)throw Error('连接信息不可用');return await Promise.race([response.json(),cancelled]);}
   finally{clearTimeout(timer);}
  }
  async function poll(){
   if(pending||document.hidden)return;pending=true;
   try{
    if(proxy){try{const value=await json('/__headring/transport'),expected=location.hostname.match(/^cp-([0-9]{4})\.localhost$/);if(value.transport!=='USB'||!/^cp_[0-9]{4}$/.test(value.device_id)||expected&&value.device_id!=='cp_'+expected[1]||!/^\d{1,3}(\.\d{1,3}){3}$/.test(value.device_address))throw Error('传输信息不匹配');transport=value;}catch{transport=null;}}
    try{latest=await json('/api/phone/links');}catch{latest=null;}
   }finally{pending=false;paint();}
  }
  paint();poll();setInterval(poll,10000);return {update:paint,refresh:poll};
 };
})(window);
