/* Address/status only. No transport switch, token transfer or automatic navigation. */
(function(global){
 'use strict';
 global.createPhoneConnections=function(options){
  const root=options.root,status=document.createElement('p'),paths=document.createElement('p'),detail=document.createElement('p');
  status.id='phone-link-status';paths.id='phone-link-addresses';paths.className=detail.className='phone-hint';paths.style.whiteSpace='pre-wrap';
   detail.textContent='USB 网络共享需手动开启并手动打开目标头环地址；地址不代表手机已连接或可开始。换地址后重新认证与屏幕注册。HTTP 不加密。保持页面前台并手动检查系统锁屏设置；网页不能保证系统屏控或 STOP 可达。';root.append(status,paths,detail);
  let latest=null,pending=false;
   function paint(){
    const labels={usb_tether:'USB 网络共享',wireless:'Wi-Fi',loopback:'本机回环（不能证明 ADB）',unknown:'未知传输'};
    const kind=labels[latest?.current_transport]||'未知传输';
    status.textContent=kind+' · 配置 '+(latest?.configured_transport||'unknown')+' · '+(latest?.state||'stale')+' · '+location.host;
    options.onKind?.(kind);
    paths.textContent='目标头环：'+(latest?.device_id||'unknown')+' · 控制器 '+(latest?.controller_identity||'unknown')+' · boot '+(latest?.boot_id||'unknown')+'\n'+(latest?.links||[]).map(link=>(labels[link.kind]||'未知')+' 页面（仅地址）：'+link.origin+'/phone').join('\n');
   }
  async function poll(){
   if(pending||document.hidden)return;pending=true;const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),2500);
    try{const response=await fetch('/api/phone/links',{headers:options.headers(),cache:'no-store',signal:controller.signal});latest=response.ok?await response.json():null;paint();}catch{latest=null;paint();}finally{clearTimeout(timer);pending=false;}
  }
  paint();poll();setInterval(poll,10000);return {update:paint,refresh:poll};
 };
})(window);
