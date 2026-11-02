/* Rendering only: Pi controls countdown, START and confirmed STOP. */
(function(global){
 'use strict';
 global.createRecordingOverlay=function(options){
  const cover=document.getElementById('privacy-cover');
  cover.setAttribute('aria-label','正在同步采集状态');
  cover.innerHTML='<div id="recording-countdown" aria-live="polite">正在同步采集状态</div><svg id="recording-stop-ring" width="180" height="180" viewBox="0 0 180 180" hidden aria-hidden="true"><circle cx="90" cy="90" r="78" fill="none" stroke="#263336" stroke-width="8"/><circle id="recording-stop-progress" cx="90" cy="90" r="78" pathLength="1" fill="none" stroke="#8edcc9" stroke-width="8" stroke-dasharray="1" stroke-dashoffset="1" transform="rotate(-90 90 90)"/></svg><p id="recording-hold-hint" hidden>长按 3 秒结束采集</p>';
  let latest=null,view=null,receivedAt=0,stopping=false,previousNumber='',lastDraw=0,animation=null;
  const title=document.getElementById('recording-countdown'),ring=document.getElementById('recording-stop-ring'),progress=document.getElementById('recording-stop-progress');
  function paint(){
   const s=view?.snapshot||latest,p=s?.privacy,canStop=view?.canStop===true;let value='正在同步采集状态';
   if(p?.stop_confirmed)stopping=false;
   if(!canStop)value=view?.stale&&s?'正在恢复连接，请稍候':'正在同步采集状态';
   else if(stopping||p?.phase==='stopping')value='正在停止采集';
   else if(view?.stale)value='正在确认采集状态';
   else if(s?.state==='countdown'&&s.countdown?.deadline_unix_ms){
    const serverNow=(s.server_time||Date.now()/1000)*1000+(performance.now()-receivedAt);
    value=String(Math.max(1,Math.min(5,Math.ceil((s.countdown.deadline_unix_ms-serverNow)/1000))));
   }else if(s?.state==='recording'||p?.phase==='recording')value='采集中';
   else if(p?.phase==='arming')value='准备倒计时';
   else if(p?.phase==='fault')value='状态待确认';
   else if(view?.localStart)value='正在确认开始请求';
   const hint=document.getElementById('recording-hold-hint');hint.hidden=!canStop;
   cover.setAttribute('role',canStop?'button':'status');cover.setAttribute('tabindex',canStop?'0':'-1');
   cover.setAttribute('aria-label',canStop?'长按三秒结束采集':value);
   if(!canStop){ring.toggleAttribute('hidden',true);progress.style.strokeDashoffset='1';}
   if(value!==previousNumber){title.textContent=value;previousNumber=value;}
   title.classList.toggle('is-number',/^[1-5]$/.test(value));
  }
  function animate(){const needed=latest?.state==='countdown'&&!document.hidden&&!cover.hidden;if(!needed){if(animation!==null)cancelAnimationFrame(animation);animation=null;return;}if(animation===null)animation=requestAnimationFrame(loop);}
  function loop(now){animation=null;if(now-lastDraw>50){paint();lastDraw=now;}animate();}
  document.addEventListener('visibilitychange',animate);
  return {update(value){latest=value;receivedAt=performance.now();if(value.privacy?.stop_confirmed)stopping=false;paint();animate();},
   state(value){view=value;paint();animate();},
   progress(value){ring.toggleAttribute('hidden',value<=0);progress.style.strokeDashoffset=String(1-value);document.getElementById('recording-hold-hint').textContent=value>0?'保持按住，松手取消':'长按 3 秒结束采集';},
   commit(){stopping=true;paint();}};
 };
})(window);
