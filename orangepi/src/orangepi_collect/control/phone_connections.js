/* Wi-Fi address/status only. No transport switch or automatic navigation. */
(function(global){
 'use strict';
 global.createPhoneConnections=function(options){
  const root=options.root,status=document.createElement('p'),paths=document.createElement('p'),detail=document.createElement('p');
  status.id='phone-link-status';paths.id='phone-link-addresses';paths.className=detail.className='phone-hint';paths.style.whiteSpace='pre-wrap';
  detail.textContent='手机与 Pi 连接同一 Wi-Fi 或手机热点。预览画质只改变显示，不改变录制分辨率。';root.append(status,paths,detail);
  let latest=null,pending=false;
  function paint(){status.textContent='Wi-Fi 无线连接 · '+location.host;options.onKind?.('Wi-Fi');paths.textContent=(latest?.links||[]).map(link=>'Wi-Fi 页面：'+link.origin+'/phone').join('\n');}
  async function poll(){
   if(pending||document.hidden)return;pending=true;const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),2500);
   try{const response=await fetch('/api/phone/links',{headers:options.headers(),cache:'no-store',signal:controller.signal});if(response.ok)latest=await response.json();paint();}catch{}finally{clearTimeout(timer);pending=false;}
  }
  paint();poll();setInterval(poll,10000);return {update:paint,refresh:poll};
 };
})(window);
