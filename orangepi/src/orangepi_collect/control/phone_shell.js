/* Page content follows source/ui/app/panel.c and panel_workflow.c. */
(function(global){
 'use strict';
 global.describePhoneRecordingResult=function(value,label){
  const review=value.review||{},last=value.last_completed||{},current=(value.session||'').split('/').pop()||'';
  const session=review.session||'',historical=!!session&&session!==current;
  const saved=!!session&&last.session===session&&last.outcome==='success';
  const title=session?(historical?(saved?'最近已保存采集结果':'最近可回看采集结果'):(saved?'本段已确认保存':'本段采集结果')):'本次结果摘要';
  const progress=value.worker?.progress||{};
  const lines=['当前流程：'+label,'当前任务标识：'+(current||'尚未准备新段')];
  if(session)lines.push((historical?'以下为历史采集结果，批次：':'本段回看批次：')+session,
   '质量检查：'+(review.qc_state==='pending'?'待主机质检'+(review.qc_scope==='emg_only'?'（肌电 '+review.qc_level+'）':''):(review.qc_level||'等待返回'))+'    ERROR：'+(review.error_count??'—')+' 条    WARN：'+(review.warn_count??'—')+' 条',
   '可回看视频：'+(review.videos||[]).length+' 路');
  else lines.push('尚无已完成收尾的回看结果。');
  for(const issue of (review.qc_issues||[]).filter(x=>x.level==='ERROR').slice(0,3))lines.push(issue.label+'：'+(issue.reason||issue.message)+(issue.action?'\n处理建议：'+issue.action:''));
  if(value.worker)lines.push('当前流程收尾进度：'+(progress.completed||0)+' / '+(progress.total||0));
  lines.push(saved?'Pi 已确认此批次保存。':'可回看不代表已确认保留；保存状态以 Pi 返回结果为准。');
  return {title,text:lines.join('\n\n'),qc_level:session?review.qc_level:null};
 };
 global.describeReferenceState=function(value){if(value?.pending_current_result)return (value.collected?'本图已有有效采集；':'')+(value.current_qc==='ERROR'?'本段 ERROR，待确认处理；不算成功':'本段已拍摄，等待检验/保存确认，尚未计为成功');if(value?.pending_qc||value?.state==='pending_qc')return '已保存 · 待主机质检（暂不重复抽取，不代表质检通过）';if(!value?.state)return '准备或选择参考图后显示状态';if(value.collected)return value.latest_qc==='ERROR'?'已有有效采集；最近一次 ERROR 不算成功':'已采集（WARN 也计为成功）';if(!value.ever_recorded)return '从未产生采集数据 · 优先采集';return '已拍 '+(value.attempt_count||0)+' 次，当前无有效成功'+(value.latest_qc?' · 最近 '+value.latest_qc:'')+(value.latest_retained?' · 未成功录像已保留':'')+(value.latest_deleted?' · 最近录像已删除':'')+(value.latest_qc==='WARN'?'（WARN本身不判失败）':'');};
 global.createPhoneShell=function({panel,options,openHistory,openTasks,openAudio,closeAux,openReference,openRecordings}){
  if(!/^\/(?:phone\/?)?$/.test(location.pathname))return null;
  const $=id=>document.getElementById(id),main=document.querySelector('main');
  const text=(node,value)=>{value=String(value??'');if(node.textContent!==value)node.textContent=value;};
  document.body.classList.add('phone-app','phone-native');
  const legacy=document.createElement('div');legacy.id='phone-legacy';legacy.hidden=true;
  for(const node of main.querySelectorAll(':scope > .hero,:scope > .flow,:scope > .footer'))legacy.append(node);
  const shell=document.createElement('div');shell.id='phone-shell';main.append(shell,legacy);
  const pages={},tabs={},names=[['overview','总览控制'],['signals','信号波形'],['preview','相机预览'],['results','录制结果'],['connections','连接设置']];
  const nav=document.createElement('nav');nav.id='phone-nav';nav.setAttribute('role','tablist');nav.setAttribute('aria-label','采集终端导航');
  for(const [id,label] of [...names,['workflow','当前采集步骤'],['fitcheck','全部相机同屏检查']]){
   const page=document.createElement('section');page.id='phone-page-'+id;page.className='phone-page';page.hidden=true;page.setAttribute('role','tabpanel');page.setAttribute('aria-label',label);pages[id]=page;shell.append(page);
   if(id==='workflow'||id==='fitcheck')continue;
   const tab=document.createElement('button');tab.textContent=label;tab.id='phone-tab-'+id;tab.setAttribute('role','tab');tab.setAttribute('aria-controls',page.id);tab.setAttribute('aria-selected','false');tab.onclick=()=>navigate(id,true);tabs[id]=tab;nav.append(tab);
  }
  const button=(label,fn)=>{const b=document.createElement('button');b.textContent=label;b.onclick=fn;return b;};
  const step=document.querySelector('.step-card'),referenceCard=document.querySelector('.reference-card'),wristCard=$('wrists').closest('.card'),emgCard=$('emg-card');
  const waves={emg_left:$('emg_left'),emg_right:$('emg_right')};
  const referenceStatus=document.createElement('section');referenceStatus.id='phone-reference-status';referenceStatus.innerHTML='<p id="phone-reference-state-text"></p><small>ERROR 可保留但不算成功；WARN 不判失败。多个启用列表合并随机抽取，优先从未采集；重采保持同一张。</small><div class="phone-row"><button id="phone-pick-reference">自选任务与参考图</button><button id="phone-switch-reference">切换下一张</button><button id="phone-manage-current">管理已拍录像</button></div><div class="phone-row" id="phone-task-choice-row" hidden><label>任务列表 <select id="phone-task-choice" aria-label="选择任务列表"></select></label><button id="phone-use-task">使用此任务列表</button></div><div class="phone-row"><select id="phone-reference-choice" hidden aria-label="选择要采集的参考图"></select><button id="phone-use-reference" hidden>使用所选图</button><button id="phone-more-references" hidden>加载更多</button></div><p id="phone-reference-choice-message" class="phone-hint"></p>';referenceCard.querySelector('.reference').append(referenceStatus);
  let referenceChoiceRevision=0,referenceChoicesNext=null,referenceChoicesBusy=false,referenceChoiceGeneration=0;
  let referenceTaskRevision=0,referenceSelectionContext='';
  async function openReferencePicker(){
   if(referenceChoicesBusy||!safe())return;
   const identity=++referenceChoiceGeneration;referenceChoicesBusy=true;
   try{
    let offset=0,rows=[],revision=null;
    do{const response=await fetch('/api/phone/tasks?'+new URLSearchParams({offset}),{headers:options.headers(),cache:'no-store'});const data=await response.json();if(!response.ok)throw Error(data.error||'任务列表读取失败');if(identity!==referenceChoiceGeneration||!safe())return;if(revision!==null&&revision!==data.revision)throw Error('任务列表已更新，请重试');revision=data.revision;rows.push(...(data.lists||[]));offset=data.next_offset;}while(offset!==null);
    const allowed=snapshot?.task_list?.allowed_task_ids||[];const select=$('phone-task-choice');select.replaceChildren();
    for(const item of rows.filter(x=>allowed.includes(x.id)&&x.sampling_enabled&&!x.hidden&&!x.import_pending&&x.count>0)){const node=document.createElement('option');node.value=item.id;node.textContent=item.name+'（剩余 '+item.remaining+' 张）';node.disabled=item.remaining===0;select.append(node);}
    const active=snapshot?.task_list?.block_task_id||snapshot?.task_list?.id;
    if([...select.options].some(x=>x.value===active))select.value=active;
    if(!select.value)throw Error('没有可选择的任务列表');referenceTaskRevision=revision;$('phone-task-choice-row').hidden=false;
   }catch(error){text($('phone-reference-choice-message'),error.message);return;}
   finally{referenceChoicesBusy=false;}
   await loadReferenceChoices();
  }
  $('phone-task-choice').onchange=()=>{referenceChoiceGeneration++;loadReferenceChoices();};
  $('phone-use-task').onclick=()=>{if(!referenceChoicesBusy&&referenceTaskRevision&&$('phone-task-choice').value)options.action('reference_list',{list_id:$('phone-task-choice').value,catalog_revision:referenceTaskRevision});};

  async function loadReferenceChoices(more=false){
   if(referenceChoicesBusy||!safe())return;const generation=referenceChoiceGeneration,cancel=new AbortController(),timeout=setTimeout(()=>cancel.abort(),5000);referenceChoicesBusy=true;$('phone-task-choice').disabled=true;const select=$('phone-reference-choice'),apply=$('phone-use-reference');apply.disabled=true;
   if(!more){select.replaceChildren();select.hidden=true;apply.hidden=true;}text($('phone-reference-choice-message'),'正在读取参考图…');
   try{const response=await fetch('/api/phone/reference-options?'+new URLSearchParams({list_id:$('phone-task-choice').value||snapshot?.task_list?.block_task_id||snapshot?.task_list?.id||'all',offset:more?referenceChoicesNext:0}),{headers:options.headers(),cache:'no-store',signal:cancel.signal});const data=await response.json();if(!response.ok)throw Error(data.error||'无法读取参考图');if(!safe()||generation!==referenceChoiceGeneration)return;
    if(more&&data.revision!==referenceChoiceRevision)throw Error('图库已变化，请重新打开自选列表');referenceChoiceRevision=data.revision;referenceChoicesNext=data.next_offset;
    for(const item of data.items||[]){const option=document.createElement('option');option.value=item.id;option.textContent=item.name+' · '+global.describeReferenceState(item.history);select.append(option);}select.hidden=false;apply.hidden=false;apply.disabled=false;$('phone-more-references').hidden=referenceChoicesNext===null;
    if(!more&&[...select.options].some(option=>option.value===snapshot?.reference_image_state?.id))select.value=snapshot.reference_image_state.id;text($('phone-reference-choice-message'),'可先更换任务列表，再使用所选参考图；阻抗检查结果保留，不会自动开录。');
   }catch(error){text($('phone-reference-choice-message'),error.message);}finally{clearTimeout(timeout);referenceChoicesBusy=false;$('phone-task-choice').disabled=!safe();}
  }
  $('phone-pick-reference').onclick=()=>openReferencePicker();$('phone-more-references').onclick=()=>loadReferenceChoices(true);
  $('phone-use-reference').onclick=()=>{if(!referenceChoicesBusy&&referenceChoiceRevision&&$('phone-reference-choice').value)options.action('select_reference',{image_id:$('phone-reference-choice').value,catalog_revision:referenceChoiceRevision,list_id:$('phone-task-choice').value||snapshot?.task_list?.block_task_id});};
  $('phone-switch-reference').onclick=()=>options.action('switch_reference');$('phone-manage-current').onclick=()=>openRecordings?.();
  const waveParents=Object.fromEntries(Object.entries(waves).map(([id,node])=>[id,node.parentElement]));
  // Old web-only review/video controls remain outside every visible native page.
  legacy.append($('review-card'),$('device-rows').closest('.card'),$('phone-install'),$('phone-awake'));
  pages.overview.innerHTML='<section class="card phone-home-state"><div><h2 id="phone-home-title">等待 Pi 状态</h2><p id="phone-home-detail">连接后显示真实采集流程</p></div><div class="phone-home-entries"></div></section><div id="phone-native-metrics"></div><div id="phone-device-list"></div><div id="phone-overview-actions" class="actions"></div><p id="phone-home-hint" class="phone-hint"></p>';
  const taskCard=document.createElement('section');taskCard.className='card phone-task-card';taskCard.innerHTML='<div class="phone-task-heading"><small>当前采集任务</small><span>每段随机抽图</span></div><b id="phone-task-name">等待任务列表</b><div class="phone-task-progress"><progress id="phone-task-progress" aria-label="参考图完成进度"></progress><small id="phone-task-counts"></small></div><p id="phone-next-step" class="phone-hint"></p>';
  pages.overview.insertBefore(taskCard,$('phone-native-metrics'));
  const dailyCard=document.createElement('section');dailyCard.className='card phone-task-card';dailyCard.id='phone-daily-collection';dailyCard.innerHTML='<small>今日合格采集时长（北京时间）</small><b id="phone-daily-duration">正在读取…</b><p id="phone-daily-detail" class="phone-hint">确认保存且通过/WARN 后累计</p>';
  const dailyExport=button('导出今日汇总（TXT）',()=>downloadDaily().catch(error=>{text($('phone-daily-detail'),error.message);}));dailyExport.id='phone-daily-export';dailyCard.append(dailyExport);pages.overview.insertBefore(dailyCard,$('phone-native-metrics'));
  const problems=document.createElement('details');problems.id='phone-device-problems';problems.hidden=true;problems.innerHTML='<summary id="phone-device-problem-summary"></summary><ul id="phone-device-problem-list"></ul><button id="phone-open-diagnostics">查看设备与 USB 诊断</button>';
  pages.overview.insertBefore(problems,$('phone-overview-actions'));
  $('phone-tasks').textContent='任务池 · 导入与隐藏';$('phone-references').hidden=true;
  pages.overview.querySelector('.phone-home-entries').append(button('采集步骤',()=>navigate('workflow',true)),$('phone-tasks'),$('phone-references'));
  for(const [id,label] of [['connected','连接设备'],['space','剩余空间'],['time','本段时长'],['saved','成功段数']]){
   const card=document.createElement('div');card.className='card phone-native-metric';const small=document.createElement('small');small.textContent=label;const value=document.createElement('b');value.id='phone-metric-'+id;value.textContent='--';card.append(small,value);$('phone-native-metrics').append(card);
  }
  const signalTabs=document.createElement('div');signalTabs.className='phone-subnav';
  for(const [id,label] of [['emg_left','左臂肌电'],['emg_right','右臂肌电']]){const b=button(label,()=>selectSignal(id));b.dataset.signal=id;signalTabs.append(b);}
  const waveHost=document.createElement('div');waveHost.id='phone-wave-host';waveHost.className='card';
  const waveHint=document.createElement('p');waveHint.id='phone-wave-hint';waveHint.className='phone-hint';
  pages.signals.append(signalTabs,waveHost,waveHint);
  const live=document.createElement('section');live.id='phone-live';
  live.innerHTML='<div id="phone-camera-tabs" role="group" aria-label="相机选择"></div><select id="phone-camera" hidden aria-label="预览相机"></select><div id="phone-live-stage"><img id="phone-live-image" hidden alt="当前相机实时预览"><p id="phone-live-empty">选择一路相机查看最新画面</p></div><div class="phone-preview-toolbar"><span id="phone-live-stats" role="status">等待相机画面</span><label>画质 <select id="phone-live-rate" aria-label="预览画质"><option value="fast">清晰流畅</option><option value="balanced">高清</option><option value="source">原图细节</option></select></label><button id="phone-wrist-mode">腕部模式</button></div>';
  pages.preview.append(live);
  const cameraNames={cam_left_wrist:'腕部候选 A',cam_right_wrist:'腕部候选 B',cam_ros_left:'头环前左',cam_ros_right:'头环前右',cam_ros_bleft:'头环后左',cam_ros_bright:'头环后右'},cameraButtons={};
  for(const [id,label] of Object.entries(cameraNames)){const opt=document.createElement('option');opt.value=id;opt.textContent=label;$('phone-camera').append(opt);const b=button(label,()=>selectCamera(id));b.dataset.camera=id;cameraButtons[id]=b;$('phone-camera-tabs').append(b);}
  $('phone-camera').value='cam_ros_left';
  pages.results.innerHTML='<section class="card phone-result-card"><h3 id="phone-result-badge">本次结果摘要</h3><div id="phone-result-text">等待 Pi 返回录制结果</div><div id="phone-result-history"></div></section>';
  $('phone-history').textContent='历史回看 · 各设备';$('phone-result-history').append($('phone-history'),$('phone-recordings'));
  pages.connections.innerHTML='<section class="card phone-connections-card"><h3>连接设置</h3><div id="phone-link-switch"></div><div id="phone-connection-detail"></div></section>';
  const workflowBar=document.createElement('div');workflowBar.className='phone-workflow-bar';workflowBar.append(button('返回总览',()=>navigate('overview',true)),button('更多控制',()=>openSettings()));
  const workflowBody=document.createElement('div');workflowBody.id='phone-workflow-body';workflowBody.append(step,referenceCard,wristCard,emgCard);pages.workflow.append(workflowBar,workflowBody);
  const referenceEntry=button('查看本段参考图',()=>openReference?.());referenceEntry.id='phone-open-reference';const abandon=button('放弃本次采集',()=>options.action('abandon'));abandon.id='phone-abandon';workflowBar.prepend(referenceEntry,abandon);
  const switchTaskList=button('更换任务列表',()=>openTasks());switchTaskList.id='phone-switch-task-list';switchTaskList.hidden=true;workflowBar.prepend(switchTaskList);
  const setupProgress=document.createElement('ol');setupProgress.id='phone-setup-progress';setupProgress.setAttribute('aria-label','采集准备步骤');setupProgress.hidden=true;
  for(const label of ['设备准备','查看参考图','开始采集']){const item=document.createElement('li');item.textContent=label;setupProgress.append(item);}pages.workflow.insertBefore(setupProgress,workflowBody);
  const setupDetails=document.createElement('details');setupDetails.id='phone-setup-details';setupDetails.hidden=true;const detailsLabel=document.createElement('summary');detailsLabel.textContent='更多操作与语音提示';setupDetails.append(detailsLabel);workflowBody.append(setupDetails);
  const startSummary=document.createElement('section');startSummary.id='phone-start-summary';startSummary.className='card';startSummary.hidden=true;startSummary.innerHTML='<h3>准备完成</h3><p id="phone-start-checks"></p><p class="phone-hint">确认佩戴与场景已准备好，再点击下方按钮开始正式采集。</p>';workflowBody.insertBefore(startSummary,setupDetails);
  const preflight=document.createElement('section');preflight.id='phone-preflight';preflight.className='card';preflight.innerHTML='<h3 id="phone-preflight-title">查看参考图</h3><p id="phone-preflight-note">请先准备本段。</p><div class="actions"><button id="phone-back-reference" hidden>返回查看参考图</button><button id="phone-check-cameras">我已看明白，检查相机</button><button id="phone-formal-start" class="primary" disabled hidden>开始正式采集</button></div><small id="phone-preflight-help">参考图可点击放大，不限查看时间。</small>';pages.workflow.append(preflight);
  const aux=document.createElement('div');aux.id='phone-aux-dialog';aux.hidden=true;aux.setAttribute('role','dialog');aux.setAttribute('aria-modal','true');aux.setAttribute('aria-label','任务与回看');
  const auxBack=button('返回',()=>{aux.hidden=true;auxOpen=false;closeAux();if(active())schedule();});auxBack.className='phone-dialog-back';aux.append(auxBack,panel);document.body.append(aux);
  const settings=document.createElement('div');settings.id='phone-settings-dialog';settings.hidden=true;settings.setAttribute('role','dialog');settings.setAttribute('aria-modal','true');settings.setAttribute('aria-label','采集控制与设置');
  const settingsBack=button('返回',()=>{settings.hidden=true;settingsOpen=false;if(active())schedule();});settingsBack.className='phone-dialog-back';
  $('settings-card').querySelector('h3').textContent='采集控制与设置';
  settings.append(settingsBack,$('settings-card'),$('phone-audio'));document.body.append(settings);
  for(const row of panel.querySelectorAll('.phone-row'))if(!row.children.length)row.remove();
  panel.querySelector('a[href="/references"]')?.remove();
  document.querySelector('.workspace').remove();document.body.append(nav);
  const header=document.querySelector('header');header.querySelector('h1').textContent='采集控制终端';
  const stateLabel=document.createElement('small');stateLabel.id='phone-header-state';header.querySelector('.brand>div:last-child').append(stateLabel);
  const linkButton=button('连接',()=>navigate('connections',true));linkButton.id='phone-transport-status';header.append(linkButton,button('设置',()=>openSettings()));
  let pageId='overview',lastState='',signalId='emg_left',snapshot=null,manualWrist=false,auxOpen=false,settingsOpen=false,fit=null;
  let setupStage='',setupContext='',referenceReadKey='';
  let problemSignature='',lastPreparationCancel=null;
  let diagnostics=null;
  let liveEpoch=0,liveController=null,liveTimer=null,liveURL='',frameId='',selectedByUser=false,pendingFrame=null,painting=false;
  let liveHandoff=Promise.resolve();
  let shownFrames=0,measuredAt=performance.now(),rate=0,currentCamera=$('phone-camera').value,lastPresentedAt=0;
  let wristController=null,wristKey='',wristVersion=0;const wristURLs=new Map();
  function stopWrists(){wristVersion++;wristController?.abort();wristController=null;wristKey='';for(const url of wristURLs.values())URL.revokeObjectURL(url);wristURLs.clear();}
  function syncWrists(){
   const wanted=pageId==='workflow'&&manualWrist&&safe()&&!auxOpen&&!settingsOpen?String(snapshot?.preview_id||''):'';
   if(!wanted){if(wristController)stopWrists();return;}if(wristController&&wristKey===wanted)return;if(wristController)stopWrists();
   const controller=new AbortController(),version=wristVersion;wristController=controller;wristKey=wanted;
   global.readPhoneFrames('/api/phone/wrist-stream?preview_id='+encodeURIComponent(wanted),{headers:options.headers(),signal:controller.signal,onFrame:async(meta,blob)=>{
    if(version!==wristVersion||!safe()||snapshot?.preview_id!==meta.preview_id||!['cam_left_wrist','cam_right_wrist'].includes(meta.camera)){controller.abort();return;}
    const url=URL.createObjectURL(blob),candidate=new Image();candidate.src=url;try{await candidate.decode();}catch(error){URL.revokeObjectURL(url);throw error;}
    if(version!==wristVersion||!safe()){URL.revokeObjectURL(url);return;}const image=document.querySelector('#'+meta.camera+' img'),previous=wristURLs.get(meta.camera);wristURLs.set(meta.camera,url);image.src=url;image.hidden=false;image.dataset.previewId=meta.preview_id;image.dataset.loadedAt=String(Date.now());if(previous)URL.revokeObjectURL(previous);options.renderWrist?.();
   }}).catch(()=>{}).finally(()=>{if(wristController===controller)wristController=null;});
  }
  const viewQuery=new URLSearchParams(location.search);
  if(cameraNames[viewQuery.get('camera')]){currentCamera=viewQuery.get('camera');selectedByUser=true;}
  if(['emg_left','emg_right'].includes(viewQuery.get('signal')))signalId=viewQuery.get('signal');
  if(['fast','balanced','source'].includes(viewQuery.get('quality')))$('phone-live-rate').value=viewQuery.get('quality');
  function safe(){return !document.hidden&&options.unlocked();}
  function release(){
   const identity=liveEpoch;
   // A late release from an old selection must never tear down the new stream.
   // All new readers wait for this barrier, including those started by update().
   liveHandoff=liveHandoff.catch(()=>{}).then(async()=>{
    if(identity!==liveEpoch)return;
    const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),2000);
    try{const response=await fetch('/api/phone/preview/release',{headers:options.headers(),cache:'no-store',signal:controller.signal});await response.arrayBuffer();}
    catch(error){}finally{clearTimeout(timeout);}
   });
   return liveHandoff;
  }
  function stopLive(clear=true){
   liveEpoch++;pendingFrame=null;clearTimeout(liveTimer);liveTimer=null;liveController?.abort();liveController=null;frameId='';
   if(clear){$('phone-live-image').hidden=true;$('phone-live-image').removeAttribute('src');if(liveURL)URL.revokeObjectURL(liveURL);liveURL='';$('phone-live-empty').textContent='等待相机画面';$('phone-live-empty').hidden=false;$('phone-live-stage').classList.remove('phone-live-stale');}
  }
  function active(){return pageId==='preview'&&!auxOpen&&!settingsOpen&&safe();}
  function schedule(delay=0){clearTimeout(liveTimer);liveTimer=null;if(active())liveTimer=setTimeout(()=>{liveTimer=null;readFrame();},delay);}
  async function paintLive(){
   if(painting)return;painting=true;
   try{while(pendingFrame){
    const item=pendingFrame;pendingFrame=null;const started=performance.now(),url=URL.createObjectURL(item.blob),candidate=new Image();candidate.decoding='async';candidate.src=url;
    try{await candidate.decode();}catch(error){URL.revokeObjectURL(url);if(item.identity===liveEpoch)throw error;continue;}
    if(item.identity!==liveEpoch||!active()){URL.revokeObjectURL(url);continue;}
    const previous=liveURL;liveURL=url;$('phone-live-image').src=url;if(previous)URL.revokeObjectURL(previous);
    $('phone-live-image').hidden=false;$('phone-live-empty').hidden=true;$('phone-live-stage').classList.remove('phone-live-stale');
    lastPresentedAt=performance.now();
    const stats=$('phone-live-stats');stats.dataset.decodeMs=(performance.now()-started).toFixed(2);stats.dataset.serverMs=String(item.meta.server_ms);stats.dataset.transport='continuous';stats.dataset.frameId=item.meta.frame_id;
    shownFrames++;const elapsed=performance.now()-measuredAt;
    if(elapsed>=1000){rate=shownFrames*1000/elapsed;shownFrames=0;measuredAt=performance.now();}
    stats.textContent=candidate.naturalWidth+' × '+candidate.naturalHeight+' · '+(rate?rate.toFixed(1)+' fps':'正在测速');
   }}catch(error){if(active())$('phone-live-stats').textContent='画面解码失败，正在同步';}finally{painting=false;}
  }
  async function readFrame(){
   if(!active()||liveController)return;const identity=liveEpoch,controller=new AbortController();liveController=controller;let nextDelay=350,watchdog=null;
   try{
    await liveHandoff;
    if(identity!==liveEpoch||!active()||controller.signal.aborted)return;
    let receivedAt=performance.now();
    // HTTP 200 without any further JPEG records used to leave liveController
    // non-null forever. Reconnect a silent stream, not just HTTP failures/EOF.
    watchdog=setInterval(()=>{if(identity===liveEpoch&&performance.now()-receivedAt>5000){clearInterval(watchdog);release();controller.abort();}},500);
    await global.readPhoneFrames('/api/phone/stream/'+currentCamera+'.jpg?quality='+$('phone-live-rate').value,{headers:options.headers(),signal:controller.signal,onFrame(meta,blob){
     if(identity!==liveEpoch||!active()){controller.abort();return;}receivedAt=performance.now();nextDelay=0;pendingFrame={blob,meta,identity};paintLive();
     }});
   }catch(error){if(identity===liveEpoch&&active()&&error.name!=='AbortError'){$('phone-live-stats').textContent=error.message;$('phone-live-stage').classList.add('phone-live-stale');}nextDelay=350;}
   finally{clearInterval(watchdog);if(liveController===controller)liveController=null;if(identity===liveEpoch)schedule(nextDelay);}
  }

  function restoreWaves(){for(const [id,node] of Object.entries(waves))if(node.parentElement!==waveParents[id])waveParents[id].append(node);}
  function selectSignal(id){
   signalId=id;restoreWaves();waveHost.append(waves[id]);
   text($('phone-wave-hint'),'肌电单位：ADC counts · 20 ms 包络，尚未换算成电压；纵轴刻度与通道数值可查看。');
   for(const b of signalTabs.children){const selected=String(b.dataset.signal===id);if(b.getAttribute('aria-pressed')!==selected)b.setAttribute('aria-pressed',selected);}
   options.drawSignal?.(id);
  }
  function selectCamera(id,user=true){
   selectedByUser=selectedByUser||user;currentCamera=id;$('phone-camera').value=id;stopLive();rate=shownFrames=0;measuredAt=performance.now();
   for(const [name,b] of Object.entries(cameraButtons))b.setAttribute('aria-pressed',String(name===id));if(active())release();schedule();
  }
  function openSettings(){if(!safe())return;settingsOpen=true;settings.hidden=false;stopLive();}
  function updateWorkflow(){
   const selectionContext=[snapshot?.session,snapshot?.task_list?.block_task_id,snapshot?.reference_image_state?.id].join('|');
   if(selectionContext!==referenceSelectionContext){referenceSelectionContext=selectionContext;referenceChoiceGeneration++;referenceChoiceRevision=0;referenceChoicesNext=null;$('phone-reference-choice').replaceChildren();$('phone-reference-choice').hidden=true;$('phone-use-reference').hidden=true;$('phone-more-references').hidden=true;const task=$('phone-task-choice'),active=snapshot?.task_list?.block_task_id;if([...task.options].some(x=>x.value===active))task.value=active;}
   $('phone-use-task').disabled=referenceChoicesBusy||!safe()||!snapshot?.actions?.includes('reference_list')||snapshot?.busy;
   $('phone-task-choice').disabled=referenceChoicesBusy||!safe()||snapshot?.busy;


   switchTaskList.hidden=!snapshot?.task_list?.manual_task_switch;switchTaskList.disabled=!safe()||!(snapshot?.actions||[]).includes('reference_list')||snapshot?.busy||options.pending?.();
   abandon.hidden=!snapshot?.actions?.includes('abandon')||['device_confirm','idle','exhausted'].includes(snapshot?.state);abandon.disabled=!safe()||snapshot?.busy||options.pending?.();referenceEntry.disabled=!safe()||!snapshot?.image||!['ready','preparing'].includes(snapshot?.state);
   text($('phone-reference-state-text'),global.describeReferenceState(snapshot?.reference_image_state)+((snapshot?.task_list?.tags||[]).length?' · 标签：'+snapshot.task_list.tags.join(' / '):''));
   $('phone-pick-reference').disabled=referenceChoicesBusy||!safe()||!snapshot?.actions?.includes('select_reference')||snapshot?.busy;
   $('phone-use-reference').disabled=referenceChoicesBusy||!referenceChoiceRevision||!safe()||!snapshot?.actions?.includes('select_reference')||snapshot?.busy;
   $('phone-switch-reference').disabled=!safe()||!snapshot?.actions?.includes('switch_reference')||snapshot?.busy;
   $('phone-manage-current').disabled=!safe()||!['device_confirm','idle','failed_wait','exhausted','confirm_stop'].includes(snapshot?.state);
   const state=snapshot?.state||'',emg=state==='emg_manual'||state==='emg_pairing';manualWrist=state.startsWith('wrist_');
   const check=snapshot?.camera_check||{},ready=state==='ready'&&check.eligible===true;
   const setup=['preparing','ready'].includes(state);
   const context=[snapshot?.system?.controller_pid,snapshot?.session,snapshot?.image,check.context].join('|');
   if(!setup||!ready||context!==setupContext)referenceReadKey='';
   setupContext=context;
   const stage=setup?(!ready?'preparing':referenceReadKey===context?'start':'reference'):'';
   if(stage!==setupStage&&pageId==='workflow'){workflowBody.scrollTop=0;pages.workflow.scrollTop=0;}setupStage=stage;
   if(setup)pages.workflow.dataset.setupStage=stage;else delete pages.workflow.dataset.setupStage;
   setupProgress.hidden=!setup;setupDetails.hidden=!setup;startSummary.hidden=stage!=='start';
   const current=stage==='preparing'?0:stage==='reference'?1:2;
   [...setupProgress.children].forEach((item,index)=>{item.classList.toggle('is-complete',index<current);if(index===current)item.setAttribute('aria-current','step');else item.removeAttribute('aria-current');});
   // Keep the original controls and event handlers available, collapsed below
   // the focused step. Calibration and all non-preparation pages retain them.
   if(setup&&step.parentElement!==setupDetails)setupDetails.append(step);
   else if(!setup&&step.parentElement!==workflowBody){workflowBody.insertBefore(step,referenceCard);setupDetails.open=false;}
   wristCard.hidden=!manualWrist;emgCard.hidden=!emg;referenceCard.hidden=setup?stage!=='reference':manualWrist||emg;
   if(pageId==='workflow')restoreWaves();
   preflight.hidden=!setup||stage==='preparing';
   $('phone-check-cameras').hidden=stage==='preparing'||stage==='start'&&!check.required;
   text($('phone-check-cameras'),stage==='start'?'重新检查相机':check.required&&!check.confirmed?'我已看明白，检查相机':'我已看明白，下一步');
   $('phone-check-cameras').disabled=!safe()||!ready||!snapshot?.image||snapshot?.busy||options.pending?.();
   $('phone-back-reference').hidden=stage!=='start';$('phone-formal-start').hidden=stage!=='start';
   $('phone-formal-start').disabled=!safe()||!ready||(check.required&&!check.confirmed)||!snapshot?.actions?.includes('start')||snapshot?.busy||options.pending?.();
   text($('phone-preflight-title'),stage==='start'?'可以开始采集':'查看任务参考图');
   text($('phone-preflight-note'),stage==='start'?'点击“开始正式采集”才进入黑屏倒计时。':'先看清任务内容、布置好场景，再继续下一步。');
   text($('phone-preflight-help'),stage==='start'?'需要再看任务时，可返回参考图。':'参考图可点击放大，不限查看时间；继续后不会自动开录。');
   text($('phone-start-checks'),'设备准备已完成'+' · 参考图已确认'+(check.required?(check.confirmed?' · 相机方向已确认':' · 等待相机方向确认'):''));
  }
  function navigate(id,user=false){
   if(!pages[id]||(user&&!safe()))return;const previous=pageId;pageId=id;main.dataset.phonePage=id;document.body.classList.toggle('phone-fit-open',id==='fitcheck');
   for(const [key,page] of Object.entries(pages))page.hidden=key!==id;
   for(const [key,tab] of Object.entries(tabs)){const chosen=key===(['workflow','fitcheck'].includes(id)?'overview':id);tab.setAttribute('aria-selected',String(chosen));tab.tabIndex=chosen?0:-1;}
   if(previous!==id){stopLive();stopWrists();diagnostics?.suspend();if(previous==='preview')release();if(previous==='fitcheck')fit?.suspend();}
   if(id==='signals')selectSignal(signalId);else restoreWaves();updateWorkflow();if(id==='preview'){if(previous!==id){rate=shownFrames=0;measuredAt=performance.now();}schedule();}if(id==='fitcheck')fit?.open();if(user)options.refresh?.();
  }
  nav.addEventListener('keydown',event=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;const index=Math.max(0,names.findIndex(([id])=>id===pageId)),n=event.key==='Home'?0:event.key==='End'?4:(index+(event.key==='ArrowRight'?1:-1)+5)%5;event.preventDefault();navigate(names[n][0],true);tabs[names[n][0]].focus();});
  function overviewActions(value){
   const supported=['confirm_devices','prepare','stop','keep','retry','rescan'],labels={confirm_devices:'确认设备，继续',prepare:'准备本段',stop:'停止采集',keep:'确认保存',retry:'重采本段',rescan:'重新检测设备'};
   const allowed=supported.filter(action=>(value.actions||[]).includes(action)&&(action!=='rescan'||(value.devices||[]).some(device=>!device.connected))).slice(0,4),container=$('phone-overview-actions');
   for(const node of [...container.children])if(!allowed.includes(node.dataset.action))node.remove();
   allowed.forEach((action,index)=>{let b=[...container.children].find(node=>node.dataset.action===action);if(!b){b=button(labels[action],()=>options.action(action));b.dataset.action=action;}
    text(b,action==='prepare'?global.piPrepareLabel(value):labels[action]);
    b.classList.toggle('danger',action==='stop');b.classList.toggle('primary',action!=='stop');const disabled=!safe()||value.busy||options.pending?.();if(b.disabled!==!!disabled)b.disabled=!!disabled;
    if(container.children[index]!==b)container.insertBefore(b,container.children[index]||null);
   });
  }
  function clock(seconds){seconds=Math.max(0,Math.floor(seconds||0));return [Math.floor(seconds/3600),Math.floor(seconds/60)%60,seconds%60].map(n=>String(n).padStart(2,'0')).join(':');}

  async function downloadDaily(day='today'){
   const response=await fetch('/api/phone/cloud/daily-report?'+new URLSearchParams({day}),{headers:options.headers(),cache:'no-store'});
   if(!response.ok){const value=await response.json();throw Error(value.error||'导出失败，请稍后重试');}
   const blob=await response.blob(),url=URL.createObjectURL(blob),link=document.createElement('a');
   link.href=url;link.download='collection-summary-'+(day==='today'?snapshot?.daily_collection?.day||'today':day)+'.txt';document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),30000);
  }

  function updateContent(value){
   const daily=value.daily_collection||{};text($('phone-daily-duration'),daily.ready?clock(daily.total_seconds):'正在读取…');
   text($('phone-daily-detail'),daily.error?'统计更新异常，显示上次结果：'+daily.error:daily.ready?(daily.day+' · '+daily.qualified_count+' 段（通过 '+daily.pass_count+' / WARN '+daily.warn_count+'） · 确认保存后计入'):'正在核对已保存数据');
   dailyExport.disabled=!safe()||!['device_confirm','idle','failed_wait','exhausted','confirm_stop'].includes(value.state);
   const label=$('title').textContent;text($('phone-home-title'),label);text(stateLabel,label);
   const deviceHint=value.state==='device_confirm'?'确认将使用当前扫描到的已连接设备，请先核对下方清单':value.state==='discarding'?'正在删除本段文件，随后重新准备同一任务':value.busy?'Pi 正在执行流程，请等待':value.state==='finalizing'?'文件收尾与 QC：'+(value.worker?.progress?.completed||0)+' / '+(value.worker?.progress?.total||'—'):'采集流程由 Orange Pi 统一管理';
   const truth=global.describePiCaptureState(value);
   text($('phone-home-detail'),['finalizing','confirm_stop','recording'].includes(value.state)?truth.detail:deviceHint);
   const devices=value.devices||[],chosen=value.selected_devices||[];
   const task=value.task_list||{};let total=task.total,remaining=task.remaining;
   if(task.manual_task_switch&&Number.isSafeInteger(task.block_size)&&Number.isSafeInteger(task.block_completed)){total=task.block_size;remaining=Math.max(0,total-task.block_completed);}
   const referenceHint=$('phone-reference-status')?.querySelector('small');if(referenceHint&&task.selection_scope==='single_list')referenceHint.textContent='ERROR 可保留但不算成功；WARN 不判失败。仅在当前列表内随机抽取，完成后手动切换；重采保持同一张。';
   text($('phone-task-name'),task.name||'请先选择参考图列表');
   $('phone-task-name').title=(task.tags||[]).join(' · ');
   const countsValid=Number.isSafeInteger(total)&&Number.isSafeInteger(remaining)&&total>=0&&remaining>=0&&remaining<=total;
   text($('phone-task-counts'),countsValid?'已完成 '+(total-remaining)+' · 剩余 '+remaining+' / '+total:'等待 Pi 返回进度');
   const taskProgress=$('phone-task-progress');taskProgress.max=countsValid?Math.max(1,total):1;if(countsValid)taskProgress.value=total-remaining;else taskProgress.removeAttribute('value');
   const nextStep=value.state!=='device_confirm'&&value.reference_image_state?.manual&&value.reference_image_state.collected&&!value.worker?.pid?'已手选已采集图，可点击“重采所选图”主动重采。':value.state==='device_confirm'?'下一步：核对已连接设备，再点击“确认设备，继续”。':value.state==='confirm_stop'?'下一步：确认保存或重采；保存确认完成后才会换下一张图。':value.state==='exhausted'||countsValid&&remaining===0&&!value.worker?.pid?'当前列表已无待采集图，请上传新图或切换任务列表。':value.state==='failed_wait'?'下一步：查看错误提示，处理后点击“准备本段”重试原图。':value.state==='idle'?'下一步：点击“'+global.piPrepareLabel(value)+'”，抽取并准备任务；此时尚未正式录制。':value.state.startsWith('wrist_')||['emg_manual','emg_pairing','preparing','ready'].includes(value.state)?'下一步：进入“采集步骤”，按当前提示继续。':value.state==='finalizing'?'正在收尾与质量检查，请等待 Pi 返回保存结果。':'按当前采集步骤继续，页面状态由 Pi 确认。';
   text($('phone-next-step'),nextStep);
   text($('phone-metric-connected'),devices.filter(d=>d.connected).length+' / '+devices.length);text($('phone-metric-space'),value.storage?.free_gb==null?'--':Number(value.storage.free_gb).toFixed(1)+' GB');
   text($('phone-metric-time'),clock(value.state==='recording'&&value.recording_since?value.server_time-value.recording_since:value.duration));text($('phone-metric-saved'),value.success_count||0);
   const list=$('phone-device-list'),names=new Set(devices.slice(0,10).map(d=>d.name));for(const row of [...list.children])if(!names.has(row.dataset.device))row.remove();
   devices.slice(0,10).forEach(device=>{let row=[...list.children].find(node=>node.dataset.device===device.name);if(!row){row=document.createElement('div');row.dataset.device=device.name;list.append(row);}row.classList.toggle('phone-device-offline',!device.connected);
    text(row,(device.connected?(device.role_confirmed===false?'已连接·左右待确认':'已连接'):'未连接')+(value.selected_devices?'·'+(chosen.includes(device.name)?'已选':'未选'):'')+'  '+device.label);
   });
   const issues=devices.filter(device=>!device.connected||device.role_confirmed===false),signature=JSON.stringify(issues.map(device=>[device.name,device.label,device.error,device.connected,device.role_confirmed,chosen.includes(device.name)]));
   problems.hidden=!issues.length;text($('phone-device-problem-summary'),'设备提示（'+issues.length+' 项）');
   if(signature!==problemSignature){problemSignature=signature;const target=$('phone-device-problem-list');target.replaceChildren();for(const device of issues){const item=document.createElement('li');item.textContent=device.label+'：'+(!device.connected?(device.error||'未发现设备，请检查 USB 连接和供电'):'已连接，请在采集步骤确认左右')+(value.selected_devices&&!chosen.includes(device.name)?'；本段未选，不参与录制':'');target.append(item);}}
   overviewActions(value);text($('phone-home-hint'),(value.errors||[]).join('；')||(!value.actions?.some(a=>['confirm_devices','prepare','start','stop','keep','retry'].includes(a))?'请进入当前采集步骤，按提示继续设置或标定。':value.selected_devices?'本段已选 '+chosen.length+' 台；未选设备不参与本段。':'预览独立于录制；停止后还需完成收尾与保存确认。'));
   const result=global.describePhoneRecordingResult(value,label);
   text($('phone-result-badge'),result.title);$('phone-result-badge').classList.toggle('phone-qc-error',result.qc_level==='ERROR');$('phone-result-badge').classList.toggle('phone-qc-warn',result.qc_level==='WARN');text($('phone-result-text'),result.text);
   text($('phone-wave-hint'),'肌电单位：ADC counts · 20 ms 包络，尚未换算成电压；纵轴刻度与通道数值可查看。');
   for(const b of signalTabs.children)text(b,value.emg_calibration?.valid?(b.dataset.signal==='emg_left'?'左臂肌电':'右臂肌电'):(b.dataset.signal==='emg_left'?'肌电候选 A':'肌电候选 B'));
   text($('phone-connection-detail'),'Pi 地址：'+location.origin+'\n\n连接：'+(safe()?'在线':'未连接或过期')+'\n\n权限：控制已启用\n\n认证：'+(options.requiresCode?'已配置访问码':'当前设备无需访问码')+'\n\n左右确认与肌电插序请进入总览的当前采集步骤。');
  }
  $('phone-camera').onchange=()=>selectCamera($('phone-camera').value);$('phone-live-rate').onchange=()=>{stopLive();release();schedule();};$('phone-wrist-mode').onclick=openSettings;
  const initial=new URLSearchParams(location.search).get('page');navigate(pages[initial]?initial:'overview');selectCamera(currentCamera,false);
  const fitEntry=button('全部相机同屏检查',()=>navigate('fitcheck',true));fitEntry.id='phone-fit-entry';live.querySelector('.phone-preview-toolbar').append(fitEntry);
  fit=global.createCameraFitCheck?.({root:pages.fitcheck,active:()=>pageId==='fitcheck',...options,back:()=>navigate('workflow',true),refresh:options.refresh,confirmed:()=>navigate('workflow',true)});
  $('phone-check-cameras').onclick=()=>{if(!safe()||$('phone-check-cameras').disabled)return;referenceReadKey=setupContext;if(snapshot?.camera_check?.required&&(setupStage==='start'||!snapshot.camera_check.confirmed))navigate('fitcheck',true);else updateWorkflow();};
  $('phone-back-reference').onclick=()=>{referenceReadKey='';updateWorkflow();};$('phone-formal-start').onclick=()=>options.action('start');
  if(pageId==='fitcheck')fit?.open();
  const connections=global.createPhoneConnections?.({root:$('phone-link-switch'),...options,page:()=>pageId,view:()=>({page:pageId,camera:currentCamera,signal:signalId,quality:$('phone-live-rate').value}),onKind:name=>{text(linkButton,name);linkButton.setAttribute('aria-label','当前连接：'+name+'，打开连接设置')}});
  const diagnosticRoot=document.createElement('section');pages.connections.querySelector('.phone-connections-card').insertBefore(diagnosticRoot,$('phone-connection-detail'));
  const headphoneRoot=document.createElement('section');headphoneRoot.id='phone-headphone-panel';headphoneRoot.className='card';pages.connections.prepend(headphoneRoot);
  const headphoneSummary=document.createElement('section');headphoneSummary.className='card phone-headphone-summary';pages.overview.insertBefore(headphoneSummary,$('phone-native-metrics'));
  const headphones=global.createPhoneHeadphones?.({root:headphoneRoot,summary:headphoneSummary,headers:options.headers,status:()=>snapshot||options.status(),unlocked:options.unlocked,
   active:()=>['overview','connections'].includes(pageId)&&!auxOpen&&!settingsOpen,open:()=>openHeadphones()});
  function openHeadphones(){closeAux();navigate('connections',true);headphones?.refresh();headphoneRoot.scrollIntoView({block:'start'});}
  diagnostics=global.createPhoneDiagnostics?.({root:diagnosticRoot,headers:options.headers,status:options.status,unlocked:options.unlocked,active:()=>pageId==='connections'&&!auxOpen&&!settingsOpen});
  $('phone-open-diagnostics').onclick=()=>{navigate('connections',true);diagnostics?.open();};
  return {
   openHeadphones,
   pause(){stopLive();stopWrists();fit?.suspend();diagnostics?.suspend();headphones?.suspend();},
   referenceViewing(){return safe()&&pageId==='workflow'&&setupStage==='reference'&&!auxOpen&&!settingsOpen;},
   displayOptions(){return {review:pageId==='results'||snapshot?.state==='confirm_stop',cameras:safe()&&!auxOpen&&!settingsOpen&&(pageId==='preview'||pageId==='fitcheck'||manualWrist),camera:pageId==='preview'?currentCamera:null};},
   beforeStart(){if(pageId!=='workflow'||setupStage!=='start'||snapshot?.camera_check?.required&&!snapshot.camera_check.confirmed){navigate('workflow');return false;}return true;},
   openCameraCheck(){navigate('fitcheck',true);},
   allowLegacyFrame(name){return pageId==='workflow'&&manualWrist&&name.startsWith('cam_')&&document.getElementById(name)?.getClientRects().length>0;},
   suspend(){referenceChoiceGeneration++;referenceChoiceRevision=0;$('phone-reference-choice').replaceChildren();$('phone-reference-choice').hidden=true;$('phone-use-reference').hidden=true;$('phone-more-references').hidden=true;stopLive();stopWrists();release();fit?.suspend();diagnostics?.suspend();headphones?.suspend();aux.hidden=true;settings.hidden=true;auxOpen=settingsOpen=false;},
   showAux(mode){
    if(mode==='reference'){navigate('workflow');return;}
    stopLive();auxOpen=true;aux.hidden=false;panel.querySelector('h3').textContent=mode==='recordings'?'采集管理':mode==='references'?'参考图库':mode==='tasks'?'任务列表':mode==='audio'?'蓝牙耳机':'历史回看 · 各设备';
    text($('phone-help'),mode==='recordings'?'可多选或上传今日保留数据；上传核验后清理本地副本会保留任务完成记录。作废删除才会释放参考图。':mode==='references'?(snapshot?.task_list?.selection_scope==='single_list'?'只在选定列表内随机抽取未完成条目；完成后手动换列表，重采保持原图。':'多个勾选列表合并抽取，完成过的图排除，重采保持原图。隐藏区仅供查看。'):mode==='replay'?'选择采集段，查看各设备的落盘内容。':'操作结果由 Pi 确认。');
   },
   update(value){
    snapshot=value;const state=value.state||'';updateContent(value);
    if(value.preparation_cancelled_at&&value.preparation_cancelled_at!==lastPreparationCancel){lastPreparationCancel=value.preparation_cancelled_at;referenceReadKey='';navigate('overview');}
    fit?.update(value);
    if(!auxOpen&&!settingsOpen&&state!==lastState){
     const guide=state.startsWith('wrist_')||['emg_manual','emg_pairing','preparing','ready'].includes(state);
     if(state==='device_confirm')navigate('overview');else if(guide||state==='confirm_stop')navigate('workflow');
    }
    lastState=state;updateWorkflow();document.querySelectorAll('[data-action=prepare]').forEach(b=>text(b,global.piPrepareLabel(value)));
    const liveNames=Object.keys(cameraNames).filter(name=>value.previews?.[name]?.live);if(!selectedByUser&&liveNames.length&&!liveNames.includes(currentCamera))selectCamera(liveNames[0],false);
    for(const [id,b] of Object.entries(cameraButtons)){
     const label=(value.devices||[]).find(d=>d.name===id)?.label||cameraNames[id];text(b,label);const pressed=String(id===currentCamera);if(b.getAttribute('aria-pressed')!==pressed)b.setAttribute('aria-pressed',pressed);
    }
    connections?.update(value);headphones?.update();diagnostics?.update();syncWrists();if(active()){if(liveURL&&performance.now()-lastPresentedAt>2500)$('phone-live-stage').classList.add('phone-live-stale');if(!liveController&&liveTimer===null)schedule();}else if(liveController)stopLive();
   }
  };
 };
})(window);

/* PI_CAPTURE_STATUS_FIX=20261105.4 */
