/* UI descriptions only; acquisition and STOP confirmation remain owned by Pi. */
(function(global){
 'use strict';
 global.piPrepareLabel=function(s){
  if(s?.last_completed?.outcome==='pending_qc'&&s?.state==='idle')return '准备下一段';
  if(s?.reference_image_state?.manual&&s.reference_image_state.collected)return '重采所选图';
  if(s?.state==='idle'&&s.last_completed?.outcome==='success')return '准备下一段';
  return s?.state==='failed_wait'?'重新准备本段':'准备本段';
 };
 global.describePiCaptureState=function(s,options={}){
  const p=s?.privacy;
  const stopped=p?.phase==='idle'&&p.required===false&&p.stop_confirmed===true
   &&!['recording','countdown'].includes(s?.state)&&s?.worker?.recording_active!==true;
  let title='正在同步采集状态',detail='尚未取得 Pi 确认的当前状态。';
  if(options.stale){
   title='连接刷新中 · 采集状态待确认';
   detail='尚未确认当前是否停止，请等待 Pi 返回状态。'+(p?.phase==='recording'?'上次确认：采集中。':p?.phase==='stopping'?'上次确认：正在停止。':'');
  }else if((s?.state==='finalizing'||s?.state==='confirm_stop')&&!stopped){
   title='停止状态待确认';detail='正在核对 Pi 的停止回执，暂不显示保存完成。';
  }else if(stopped&&s?.state==='finalizing'){
   const w=s.worker||{},p=w.progress||{},phase=w.state;
   const started=Number(w.processing_started),elapsed=started>0?Math.max(0,(s.server_time||s.controller_time||w.time||started)-started):null;
   const timing=elapsed===null?'':' · 已用 '+Math.floor(elapsed/60)+' 分 '+Math.floor(elapsed%60)+' 秒';
   title=phase==='checking'?(w.qc_scope==='emg_only'?'肌电质检中':'质量检查中'):phase==='postprocessing'?'数据整理中':'文件保存中';
   const labels={emg_left:'左肌电',emg_right:'右肌电',board_imu:'板载惯性数据',marker:'起止标记',cam_left_wrist:'左腕相机',cam_right_wrist:'右腕相机',cam_ros_left:'头环前左',cam_ros_right:'头环前右',cam_ros_bleft:'头环后左',cam_ros_bright:'头环后右'};
   detail=(phase==='checking'?'已完成 '+(p.completed||0)+' / '+(p.total||'—')+' 项'+(p.active?.length?' · 正在检查 '+p.active.map(name=>labels[name]||name).join('、'):''):phase==='postprocessing'?'正在整理肌电与时间戳':'正在确认文件已落盘')+timing;
   if(w.qc_scope==='emg_only')detail+='；其他模态待 Linux 主机质检。';
  }else if(stopped&&s?.state==='confirm_stop'){
   title='已停止采集 · 等待保存确认';detail='本段文件已暂存，请确认保存或重采；确认保存后继续整理和质检。';
  }else if(p?.phase==='stopping'){
   title='正在停止采集 · 等待停止确认';detail='Pi 正在执行停止，尚未确认全部采集已停止。';
  }else if(options.localStop&&!stopped){
   title='停止请求已发送 · 等待 Pi 确认';detail='发送请求不代表已经停止，请等待设备回执。';
  }else if(s?.state==='recording'||p?.phase==='recording'||s?.worker?.recording_active===true){
   title='采集中';detail='Pi 确认仍在录制。';
  }else if(stopped&&s?.state==='idle'&&['success','pending_qc'].includes(s.last_completed?.outcome)){
   title='本段已保存 · 可采集下一段';detail='点击“准备下一段”抽取并准备下一任务，完成检查后再开始正式录制。';
  }
  return {title,detail,stopped:!options.stale&&stopped};
 };
})(window);

/* Rendering only: Pi controls countdown, START and confirmed STOP. */
(function(global){
 'use strict';
 global.createRecordingOverlay=function(options){
  const cover=document.getElementById('privacy-cover');
  const qcCard=document.createElement('section');qcCard.id='qc-progress-card';qcCard.hidden=true;
  qcCard.setAttribute('aria-live','polite');qcCard.style.cssText='margin:12px 18px;padding:16px;border:1px solid #527a74;border-radius:12px;background:#12232a;color:#ecf9f4';
  qcCard.innerHTML='<b id="qc-stage-title"></b><p id="qc-stage-detail"></p><progress id="qc-stage-progress" style="width:100%"></progress>';
  document.querySelector('header').insertAdjacentElement('afterend',qcCard);
  cover.setAttribute('aria-label','正在同步采集状态');
  cover.innerHTML='<div id="recording-countdown" aria-live="polite">正在同步采集状态</div><svg id="recording-stop-ring" width="180" height="180" viewBox="0 0 180 180" hidden aria-hidden="true"><circle cx="90" cy="90" r="78" fill="none" stroke="#263336" stroke-width="8"/><circle id="recording-stop-progress" cx="90" cy="90" r="78" pathLength="1" fill="none" stroke="#8edcc9" stroke-width="8" stroke-dasharray="1" stroke-dashoffset="1" transform="rotate(-90 90 90)"/></svg><p id="recording-hold-hint" hidden>长按 3 秒结束采集</p><button id="recording-discard" data-no-hold hidden style="position:relative;z-index:2;margin-top:36px;background:#392328;color:#ffe7e7;border:1px solid #a67777;padding:14px 20px;border-radius:10px">停止并放弃本段</button>';
  let latest=null,view=null,receivedAt=0,stopping=false,renderFault='',previousNumber='',lastDraw=0,animation=null,modeTarget=null;
  const title=document.getElementById('recording-countdown'),ring=document.getElementById('recording-stop-ring'),progress=document.getElementById('recording-stop-progress');
  const discard=document.getElementById('recording-discard');
  discard.onclick=event=>{event.stopPropagation();if(!discard.disabled)options.discard?.(view?.snapshot||latest);};
  function paint(){
   const s=view?.snapshot||latest,p=s?.privacy,canStop=view?.canStop===true;let value='正在同步采集状态';
   if(p?.stop_confirmed)stopping=false;
   const truth=global.describePiCaptureState(s,{stale:!s||view?.stale!==false,localStop:stopping});
   value=truth.title;
   qcCard.hidden=!(s?.state==='finalizing'&&truth.stopped);
   if(!qcCard.hidden){
    document.getElementById('qc-stage-title').textContent=truth.title;
    document.getElementById('qc-stage-detail').textContent=truth.detail;
    const meter=document.getElementById('qc-stage-progress'),p=s.worker?.progress||{};
    if(Number(p.total)>0){meter.max=Number(p.total);meter.value=Number(p.completed)||0;}else meter.removeAttribute('value');
   }
   if(!view?.stale&&!stopping&&s?.state==='countdown'&&s.countdown?.deadline_unix_ms){
    const serverNow=(s.server_time||Date.now()/1000)*1000+(performance.now()-receivedAt);
    value=String(Math.max(1,Math.min(5,Math.ceil((s.countdown.deadline_unix_ms-serverNow)/1000))));
   }else if(!view?.stale&&p?.phase==='arming')value='准备倒计时';
   else if(!view?.stale&&p?.phase==='fault')value='采集异常 · 停止状态待确认';
   else if(view?.localStart)value='正在确认开始请求';
   if(modeTarget&&p?.required===false&&s?.worker?.recording_active!==true&&!['recording','countdown'].includes(s?.state))value='正在切换到'+(modeTarget==='eeg'?'脑电版':'肌电版')+'\n正在重新连接，请稍候。';
   if(renderFault)value='页面显示异常 · 请刷新网页\n'+truth.title;
   cover.dataset.captureState=truth.title;
   cover.dataset.renderFault=renderFault?'true':'false';
   // The lightweight gate can advance before a slow full-status response.
   if(view?.stale===false&&['finalizing','confirm_stop'].includes(s?.state)){
    for(const id of ['title','step-title','phone-home-title']){const node=document.getElementById(id);if(node)node.textContent=truth.title;}
    for(const id of ['instruction','phone-home-detail']){const node=document.getElementById(id);if(node)node.textContent=truth.detail;}
   }
   if(view?.stale===false&&s?.revision!==latest?.revision){
    document.querySelectorAll('[data-action],#phone-overview-actions button').forEach(button=>button.disabled=true);
   }
   discard.hidden=!canStop||s?.state!=='recording';discard.disabled=!!view?.stale||stopping;
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
  title.style.whiteSpace='pre-line';
  return {modeSwitch(target){modeTarget=['emg','eeg'].includes(target)?target:null;paint();},update(value){latest=value;receivedAt=performance.now();if(value.privacy?.stop_confirmed)stopping=false;paint();animate();},
   state(value){view=value;paint();animate();},
   progress(value){ring.toggleAttribute('hidden',value<=0);progress.style.strokeDashoffset=String(1-value);document.getElementById('recording-hold-hint').textContent=value>0?'保持按住，松手取消':'长按 3 秒结束采集';},
   renderFault(error){renderFault=error?String(error.message||error):'';paint();},
   commit(){stopping=true;paint();}};
 };
})(window);

/* PI_CAPTURE_STATUS_FIX=20261105.4 */
