/* Mobile controls for Pi-owned background uploads. Credentials never enter browser storage. */
(function (global) {
 'use strict';
 global.createPhoneCloudUploads = function ({root, request, status, message, current}) {
  let version = 0, timer = null, loading = null, busy = false, day = 'today';
  let data = null, lastRead = 0, editingCredentials = false, settingsLoaded = false;
  let confirmation = null, ui = {}, uploadSelection = new Set(), cleanupSelection = new Set();
  const recordingRows = new Map(), jobRows = new Map();
  const labels = {not_uploaded:'未上传', queued:'等待上传', checking:'检查本地文件', uploading:'正在上传', verifying:'正在核对', success:'上传完成', failed:'上传失败', paused:'已暂停', interrupted:'上传中断',cleanup_queued:'等待清理',cleaning:'正在清理本地副本'};
  const phaseLabels = {checking_source:'检查本地数据', hashing:'检查本地数据', authenticating:'验证上传账号', project_selected:'已确认上传项目', uploading:'正在上传', verifying:'核对云端文件', verifying_objects:'核对云端文件', completing:'确认平台状态', waiting_collection:'等待采集结束', success:'上传完成', failed:'上传失败', paused:'已暂停', interrupted:'上传中断'};
  const node = (tag, text, cls) => {const e=document.createElement(tag); if(text!==undefined&&text!==null)e.textContent=String(text); if(cls)e.className=cls; return e;};
  const button = (text, fn, cls) => {const e=node('button',text,cls); e.type='button'; e.onclick=fn; return e;};
  const number = value => Math.max(0, Number(value)||0);
  const bytes = value => {let n=number(value),i=0; const units=['B','KiB','MiB','GiB','TiB']; while(n>=1024&&i<units.length-1){n/=1024;i++;} return (i?n.toFixed(n>=100?0:1):Math.floor(n))+' '+units[i];};
  const active = () => !root.hidden && !document.hidden && Boolean(current());
  const valid = id => id===version && active();
  const tell = text => {if(active())message(text);};
  const errorTake = item => String(item.qc_level||'').toUpperCase().includes('ERROR');
  const stateOf = item => item.cloud?.state || 'not_uploaded';
  const canUpload = item => Boolean(item.retained && item.token && !item.cloud?.local_deleted && stateOf(item)==='not_uploaded');
  const canCleanup = item => item.cloud?.can_cleanup===true && !item.cloud?.local_deleted && Boolean(item.cloud?.job_id);
  const selectedRecordings = () => (data?.recordings||[]).filter(item=>uploadSelection.has(String(item.id))&&canUpload(item));
  const cleanupRecordings = () => (data?.recordings||[]).filter(item=>cleanupSelection.has(String(item.cloud?.job_id))&&canCleanup(item));

  async function json(path, body) {
   const response=await request('/api/phone/cloud'+path, body===undefined?{timeoutMs:15000}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),timeoutMs:15000});
   let result;
   try{result=await response.json();}
   catch(error){throw Error(response.ok===false?'操作失败，请重试。':'未收到有效结果，请刷新核对状态后重试。');}
   if(!result||typeof result!=='object'||Array.isArray(result))throw Error('返回结果异常，请刷新核对状态。');
   if(response.ok===false||result?.ok===false)throw Error(result?.error||result?.message||'操作失败，请重试。');
   return result;
  }
  function schedule(delay=2000) {
   clearTimeout(timer); timer=null;
   if(active()&&!busy&&!loading)timer=setTimeout(()=>{timer=null;if(active())load();},delay);
  }
  function controls() {
   if(!ui.toolbar)return;
   const configured=Boolean(data?.credentials?.configured), collectionBusy=Boolean(data?.collection_busy);
   ui.range.disabled=busy; ui.refresh.disabled=busy||Boolean(loading);
   ui.selectPending.disabled=busy||!(data?.recordings||[]).some(item=>canUpload(item)&&!errorTake(item));
   ui.clearSelection.disabled=busy||!uploadSelection.size;
   ui.uploadSelected.disabled=busy||!configured||!selectedRecordings().length;
   ui.uploadSelected.textContent='上传已选 '+selectedRecordings().length+' 批';
   ui.uploadToday.disabled=busy||!configured;
   ui.selectCleanup.disabled=busy||collectionBusy||!(data?.recordings||[]).some(canCleanup);
   ui.cleanup.disabled=busy||collectionBusy||!cleanupRecordings().length;
   ui.cleanup.textContent='清理已选 '+cleanupRecordings().length+' 批本地数据';
   ui.pause.disabled=busy||!data; ui.pause.textContent=data?.paused?'继续上传队列':'暂停上传队列';
   ui.saveCredentials.disabled=busy||collectionBusy;
   ui.changeCredentials.disabled=busy||collectionBusy;
   ui.clearCredentials.disabled=busy||collectionBusy||!configured;
   for(const e of [ui.ak,ui.sk,ui.project,ui.target,ui.task])e.disabled=busy||collectionBusy;
   for(const row of recordingRows.values()){
    row.upload.disabled=busy||!canUpload(row.item);
    row.cleanup.disabled=busy||collectionBusy||!canCleanup(row.item);
   }
   for(const row of jobRows.values())row.retry.disabled=busy||collectionBusy||!configured||!['failed','interrupted'].includes(row.item.state)||row.item.local_deleted===true;
   if(ui.confirmAction)ui.confirmAction.disabled=busy||(confirmation?.danger&&collectionBusy);
   if(ui.confirmCancel)ui.confirmCancel.disabled=busy;
  }
  function progress(target, completed, total) {
   const maximum=number(total),value=Math.min(number(completed),maximum);
   target.max=Math.max(1,maximum); target.value=value;
   target.setAttribute('aria-valuetext',bytes(value)+' / '+bytes(maximum));
  }
  function drawRecording(item) {
   const id=String(item.id); let row=recordingRows.get(id);
   if(!row){
    const article=node('article',null,'pcu-card'), title=node('b'), info=node('p'), state=node('p'), choices=node('div',null,'pcu-actions');
    const upload=node('input'),cleanup=node('input');upload.type=cleanup.type='checkbox';
    const uploadLabel=node('label',null,'pcu-choice'),cleanupLabel=node('label',null,'pcu-choice');
    uploadLabel.append(upload,node('span','选择上传'));cleanupLabel.append(cleanup,node('span','选择清理本地数据'));
    choices.append(uploadLabel,cleanupLabel);article.append(title,info,state,choices);ui.recordings.append(article);
    row={article,title,info,state,upload,cleanup,uploadLabel,cleanupLabel,item};recordingRows.set(id,row);
    upload.onchange=()=>{upload.checked?uploadSelection.add(id):uploadSelection.delete(id);closeConfirmation();controls();};
    cleanup.onchange=()=>{const job=String(row.item.cloud?.job_id);cleanup.checked?cleanupSelection.add(job):cleanupSelection.delete(job);closeConfirmation();controls();};
   }
   row.item=item;
   row.title.textContent=item.reference_name||id;
   row.info.textContent=[item.day||'',number(item.duration).toFixed(1)+' 秒',id].filter(Boolean).join(' · ');
   const qc=errorTake(item)?'QC_ERROR · 保留的未成功数据':(item.qc_level?'QC_'+String(item.qc_level).toUpperCase():'QC 待确认');
   const cloudState=item.cloud?.local_deleted?'云端已上传 · 本地已清理':labels[stateOf(item)]||'上传状态待确认';
   row.state.textContent=qc+' · '+cloudState+(canCleanup(item)?' · 已核对，可清理本地数据':'');
   row.state.className=errorTake(item)?'pcu-warning':'';
   row.uploadLabel.hidden=!canUpload(item);row.cleanupLabel.hidden=!canCleanup(item);
   if(!canUpload(item))uploadSelection.delete(id);
   if(!canCleanup(item))cleanupSelection.delete(String(item.cloud?.job_id));
   row.upload.checked=uploadSelection.has(id);row.cleanup.checked=cleanupSelection.has(String(item.cloud?.job_id));
  }
  function drawJob(item) {
   const id=String(item.id); let row=jobRows.get(id);
   if(!row){
    const article=node('article',null,'pcu-card'),title=node('b'),state=node('p'),bar=node('progress'),counts=node('p'),file=node('p'),error=node('p',null,'pcu-error'),remote=node('p'),retry=button('重试上传',()=>retryJob(id));
    bar.setAttribute('aria-label','本批上传字节进度');article.append(title,state,bar,counts,file,error,remote,retry);ui.jobs.append(article);
    row={article,title,state,bar,counts,file,error,remote,retry,item};jobRows.set(id,row);
   }
   row.item=item;row.title.textContent=item.session_id||id;
   row.state.textContent=(labels[item.state]||'上传状态待确认')+(phaseLabels[item.phase]&&phaseLabels[item.phase]!==labels[item.state]?' · '+phaseLabels[item.phase]:'')+(item.local_deleted?' · 本地已清理':'');
   progress(row.bar,item.completed_bytes,item.total_bytes);
   row.counts.textContent=Math.min(number(item.completed_files),number(item.total_files))+' / '+number(item.total_files)+' 个文件 · '+bytes(Math.min(number(item.completed_bytes),number(item.total_bytes)))+' / '+bytes(item.total_bytes);
   row.file.textContent=item.current_file?'当前文件：'+item.current_file:'';row.file.hidden=!item.current_file;
   row.error.textContent=item.error?'原因：'+item.error:'';row.error.hidden=!item.error;
   row.remote.textContent=item.remote_path?'云端位置：'+item.remote_path:'';row.remote.hidden=!item.remote_path;
   row.retry.hidden=!['failed','interrupted'].includes(item.state)||item.local_deleted===true;
  }
  function reconcile() {
   if(!data||!ui.toolbar)return;
   const recordings=Array.isArray(data.recordings)?data.recordings:[],jobs=Array.isArray(data.jobs)?data.jobs:[];
   const configured=Boolean(data.credentials?.configured);
   ui.credentialState.textContent=configured?'上传账号已保存：'+(data.credentials.masked_ak||'已配置'):'尚未设置上传账号';
   ui.credentialEditor.hidden=configured&&!editingCredentials;
   ui.changeCredentials.hidden=!configured||editingCredentials;ui.clearCredentials.hidden=!configured;
   if(!settingsLoaded){ui.project.value=data.settings?.project_name||'EGOL2';ui.target.value=data.settings?.target_parent||'EGOL2/pi-collect';ui.task.value=data.settings?.task_id??'';settingsLoaded=true;}
   const ready=recordings.filter(canUpload),errors=ready.filter(errorTake);
   ui.selectionHint.textContent=(day==='today'?'今日':'全部')+'：可上传 '+ready.length+' 批，其中 QC_ERROR '+errors.length+' 批。批量全选跳过 ERROR；可在条目中手动选择。';
   const ids=new Set(recordings.map(item=>String(item.id))),jobIds=new Set(jobs.map(item=>String(item.id)));
   for(const [id,row] of recordingRows)if(!ids.has(id)){row.article.remove();recordingRows.delete(id);uploadSelection.delete(id);}
   for(const [id,row] of jobRows)if(!jobIds.has(id)){row.article.remove();jobRows.delete(id);}
   for(const item of recordings)drawRecording(item);
   for(const item of jobs)drawJob(item);
   const eligibleCleanup=new Set(recordings.filter(canCleanup).map(item=>String(item.cloud.job_id)));
   for(const id of cleanupSelection)if(!eligibleCleanup.has(id))cleanupSelection.delete(id);
   ui.recordingsEmpty.textContent='此日期范围暂无保留采集数据。';ui.jobsEmpty.textContent='尚无上传任务。';
   ui.recordingsEmpty.hidden=recordings.length>0;ui.jobsEmpty.hidden=jobs.length>0;
   const completed=jobs.filter(item=>item.state==='success').length,failed=jobs.filter(item=>['failed','interrupted'].includes(item.state)).length;
   const total=jobs.reduce((sum,item)=>sum+number(item.total_bytes),0),done=jobs.reduce((sum,item)=>sum+Math.min(number(item.completed_bytes),number(item.total_bytes)),0);
   ui.overall.textContent=(data.collection_busy?'设备检查或采集准备中，上传等待空闲。':data.paused?'上传队列已暂停。':'上传任务在 Pi 后台执行。')+' '+completed+' / '+jobs.length+' 批完成'+(failed?'，'+failed+' 批需重试':'')+'。';
   progress(ui.overallBar,done,total);ui.overallBytes.textContent=bytes(done)+' / '+bytes(total)+' · 关闭手机页面后后台任务继续';
   ui.cleanupHint.textContent='仅“已核对，可清理本地数据”的批次允许清理。清理永久删除 Pi 本地文件，云端数据保留；不会自动清理。';
   controls();
  }
  async function load(force=false) {
   if(!active()||busy&&!force)return;
   if(loading){const waitingVersion=version;await loading;if(force&&valid(waitingVersion))return load(true);return;}
   const id=version,range=day;clearTimeout(timer);timer=null;
   const pending=(async()=>{
    try{
     const next=await json('/status?'+new URLSearchParams({day:range}));
     if(!valid(id)||range!==day)return;
     if(!Array.isArray(next.recordings)||!Array.isArray(next.jobs))throw Error('上传状态格式异常，请刷新。');
     data=next;lastRead=Date.now();ui.network.textContent='';ui.network.hidden=true;reconcile();
    }catch(error){if(valid(id)&&error.name!=='AbortError'){ui.network.textContent=error.message||'连接失败，请稍后刷新。';ui.network.hidden=false;}}
   })();loading=pending;controls();await pending;
   if(loading===pending)loading=null;
   if(valid(id)){controls();schedule();}
  }
  async function perform(action, successText) {
   if(!active()||busy)return;
   const id=version;busy=true;clearTimeout(timer);timer=null;controls();
   try{await action(id);if(valid(id)){if(successText)tell(successText);await load(true);}}
   catch(error){if(valid(id)&&error.name!=='AbortError')tell(error.message||'操作失败，请重试。');}
   finally{if(id===version){busy=false;if(active()){reconcile();schedule();}}}
  }
  function closeConfirmation() {confirmation=null;if(ui.confirmation){ui.confirmation.hidden=true;ui.confirmation.replaceChildren();ui.confirmAction=ui.confirmCancel=null;}}
  function ask(title, details, confirmLabel, action, danger=false, successText=null) {
   if(!active())return;
   closeConfirmation();confirmation={danger};
   const heading=node('b',title),description=node('p',details),actions=node('div',null,'pcu-actions');
   ui.confirmAction=button(confirmLabel,()=>perform(async(id)=>{await action(id);if(valid(id))closeConfirmation();},successText||(danger?'清理请求已提交，请等待批次显示“本地已清理”。':'上传请求已提交，请查看批次状态。')),danger?'pcu-danger':'');
   ui.confirmCancel=button('取消',closeConfirmation);actions.append(ui.confirmAction,ui.confirmCancel);
   ui.confirmation.append(heading,description,actions);ui.confirmation.hidden=false;ui.confirmation.focus();ui.confirmation.scrollIntoView({block:'nearest'});controls();
  }
  function settings() {
   const project_name=ui.project.value.trim(),target_parent=ui.target.value.trim(),task=ui.task.value.trim();
   if(!project_name||!target_parent)throw Error('请填写上传项目和云端目录。');
   if(task&&!/^-?\d+$/.test(task))throw Error('平台任务编号须为整数，未关联任务时留空。');
   const task_id=task?Number(task):null;
   if(task_id!==null&&!Number.isSafeInteger(task_id))throw Error('平台任务编号超出有效范围。');
   return {project_name,target_parent,task_id};
  }
  function confirmUpload(items, today=false) {
   if(!items.length){tell(today?'今日没有可批量上传的成功/WARN未上传数据。':'请先选择待上传批次。');return;}
   let target;try{target=settings();}catch(error){tell(error.message);return;}
   const frozen=items.map(item=>({session_id:item.id,token:item.token})),errors=items.filter(errorTake).length;
   const names=items.slice(0,8).map(item=>item.reference_name||item.id).join('、')+(items.length>8?'等':'');
   ask('确认上传 '+items.length+' 批',names+'。项目：'+target.project_name+'；目录：'+target.target_parent+'。'+(errors?'包含 '+errors+' 批 QC_ERROR 未成功数据，请确认需要上传。':'仅上传已选择的保留数据。')+' 上传完成前不会删除本地数据。', '确认加入上传队列',async(id)=>{await json('/queue',{selection:frozen,settings:target});if(valid(id))uploadSelection.clear();});
  }
  async function uploadToday() {
   await perform(async(id)=>{
    const next=await json('/status?day=today');
    if(valid(id))confirmUpload((next.recordings||[]).filter(item=>canUpload(item)&&!errorTake(item)),true);
   });
  }
  function retryJob(id) {
   const item=jobRows.get(id)?.item;
   if(!item||!['failed','interrupted'].includes(item.state))return;
   perform(()=>json('/retry',{job_id:id}),'已提交重试，请查看批次状态。');
  }
  function cleanup() {
   const items=cleanupRecordings();if(!items.length)return;
   const ids=items.map(item=>String(item.cloud.job_id)),names=items.slice(0,8).map(item=>item.reference_name||item.id).join('、')+(items.length>8?'等':'');
   ask('永久清理 '+items.length+' 批本地数据？',names+'。这些批次已获得云端完成确认。此操作永久删除 Pi 上对应批次的全部本地采集文件，无法从本地恢复。请再次核对批次；云端副本保留。','确认永久删除本地 '+items.length+' 批',async(id)=>{
    const allowed=new Set((data?.recordings||[]).filter(canCleanup).map(item=>String(item.cloud.job_id)));
    if(ids.some(id=>!allowed.has(id)))throw Error('批次状态已改变，请刷新后重新选择可清理数据。');
    await json('/cleanup',{job_ids:ids,confirmation:'DELETE_UPLOADED_LOCAL'});if(valid(id))cleanupSelection.clear();
   },true);
  }
  function field(label, input) {const wrap=node('label',null,'pcu-field');wrap.append(node('span',label),input);return wrap;}
  function build() {
   root.classList.add('pcu-root');
   const style=node('style');style.textContent=`
    .pcu-root{min-width:0;max-width:100%;overflow-wrap:anywhere;box-sizing:border-box;padding-bottom:24px}
    .pcu-root *{box-sizing:border-box;min-width:0}.pcu-root [hidden]{display:none!important}
    .pcu-root button,.pcu-root input,.pcu-root select{font:inherit;max-width:100%}.pcu-root button{min-height:44px;white-space:normal;overflow-wrap:anywhere}
    .pcu-root input:not([type=checkbox]),.pcu-root select{width:100%;min-height:44px;padding:8px}.pcu-root input[type=checkbox]{width:22px;height:22px;flex:none;margin:0}
    .pcu-actions{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:10px 0}.pcu-actions>button{flex:1 1 140px}
    .pcu-card,.pcu-confirmation{border:1px solid #72859b66;border-radius:10px;padding:12px;margin:12px 0;max-width:100%}
    .pcu-card p,.pcu-confirmation p{margin:8px 0;line-height:1.55}.pcu-card b,.pcu-root small{overflow-wrap:anywhere}
    .pcu-root progress{display:block;width:100%;height:14px;margin:10px 0}.pcu-choice{display:flex;gap:8px;align-items:center;min-height:44px;cursor:pointer;flex:1 1 190px}
    .pcu-fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,240px),1fr));gap:10px}.pcu-field{display:flex;flex-direction:column;gap:6px;margin:8px 0}
    .pcu-warning{color:#bc7800}.pcu-error{color:#df4949}.pcu-danger{border-color:#d65353!important}.pcu-confirmation{border:2px solid #bc7800;outline-offset:3px}
    .pcu-root summary{cursor:pointer;min-height:44px;padding:10px 0}.pcu-root h3{margin:20px 0 8px}
    @media(max-width:440px){.pcu-actions>button{flex-basis:100%}.pcu-card{padding:10px}}
   `;root.append(style);
   const title=node('h3','数据上传'),intro=node('p','选择已保留的采集批次上传，日期按北京时间的采集开始日统计。可关闭手机页面，Pi 会保留任务与进度。');
   ui.network=node('p',null,'pcu-error');ui.network.setAttribute('role','alert');ui.network.hidden=true;
   ui.toolbar=node('div',null,'pcu-actions');ui.range=node('select');ui.range.setAttribute('aria-label','采集日期范围');
   for(const [value,text] of [['today','今日采集'],['all','全部采集']]){const option=node('option',text);option.value=value;ui.range.append(option);}
   ui.range.onchange=()=>{
    day=ui.range.value;uploadSelection.clear();cleanupSelection.clear();closeConfirmation();data=null;
    for(const row of recordingRows.values())row.article.remove();recordingRows.clear();
    for(const row of jobRows.values())row.article.remove();jobRows.clear();
    ui.recordingsEmpty.textContent=ui.jobsEmpty.textContent='正在读取…';ui.recordingsEmpty.hidden=ui.jobsEmpty.hidden=false;
    ui.selectionHint.textContent='正在读取所选日期范围…';ui.overall.textContent='正在读取上传状态…';controls();load(true);
   };
   ui.refresh=button('刷新状态',()=>load());ui.toolbar.append(ui.range,ui.refresh);
   const account=node('section',null,'pcu-card');ui.credentialState=node('b','正在读取账号状态…');
   ui.changeCredentials=button('更换 AK / SK',()=>{editingCredentials=true;reconcile();ui.ak.focus();});
   ui.clearCredentials=button('清除已保存的 AK / SK',()=>ask('清除上传账号？','清除后需要重新输入 AK / SK 才能提交新的上传任务。已保存的采集数据保留。','确认清除账号',async(id)=>{await json('/clear-credentials',{});if(valid(id)){editingCredentials=false;ui.ak.value=ui.sk.value='';}},false,'上传账号已清除。'));
   ui.credentialEditor=node('form');ui.credentialEditor.autocomplete='off';ui.ak=node('input');ui.sk=node('input');
   for(const [input,name] of [[ui.ak,'AK'],[ui.sk,'SK']]){input.type='password';input.autocomplete='off';input.spellcheck=false;input.setAttribute('autocapitalize','off');input.setAttribute('aria-label',name);input.required=true;}
   ui.saveCredentials=node('button','保存 AK / SK');ui.saveCredentials.type='submit';
   ui.credentialEditor.append(field('AK',ui.ak),field('SK',ui.sk),ui.saveCredentials,node('p','首次保存后保留在 Pi，下次无需重输；页面只显示掩码。'));
   ui.credentialEditor.onsubmit=event=>{event.preventDefault();if(busy)return;const ak=ui.ak.value.trim(),sk=ui.sk.value.trim();if(!ak||!sk){tell('请填写 AK 和 SK。');return;}ui.ak.value=ui.sk.value='';perform(async(id)=>{await json('/credentials',{ak,sk});if(valid(id))editingCredentials=false;},'上传账号已保存。');};
   const accountActions=node('div',null,'pcu-actions');accountActions.append(ui.changeCredentials,ui.clearCredentials);account.append(ui.credentialState,accountActions,ui.credentialEditor);
   const target=node('details'),targetSummary=node('summary','上传项目与目录'),fields=node('div',null,'pcu-fields');
   ui.project=node('input');ui.target=node('input');ui.task=node('input');ui.task.inputMode='numeric';ui.task.placeholder='不关联时留空';
   fields.append(field('项目名称',ui.project),field('云端父目录',ui.target),field('平台任务编号（可选）',ui.task));target.append(targetSummary,fields);
   const overview=node('section',null,'pcu-card');ui.overall=node('p','正在读取上传状态…');ui.overall.setAttribute('role','status');ui.overallBar=node('progress');ui.overallBar.setAttribute('aria-label','所有批次上传字节进度');ui.overallBytes=node('p');
   ui.pause=button('暂停上传队列',()=>perform(()=>json('/control',{action:data?.paused?'resume':'pause'}),'上传队列设置已更新。'));overview.append(ui.overall,ui.overallBar,ui.overallBytes,ui.pause);
   ui.confirmation=node('section',null,'pcu-confirmation');ui.confirmation.hidden=true;ui.confirmation.tabIndex=-1;ui.confirmation.setAttribute('aria-label','确认当前操作');
   const actions=node('div',null,'pcu-actions');ui.selectPending=button('全选待上传（跳过 ERROR）',()=>{for(const item of data?.recordings||[])if(canUpload(item)&&!errorTake(item))uploadSelection.add(String(item.id));closeConfirmation();reconcile();});
   ui.clearSelection=button('取消上传勾选',()=>{uploadSelection.clear();closeConfirmation();reconcile();});ui.uploadSelected=button('上传已选 0 批',()=>confirmUpload(selectedRecordings()));ui.uploadToday=button('上传今日成功/WARN全部未上传',uploadToday);
   actions.append(ui.selectPending,ui.clearSelection,ui.uploadSelected,ui.uploadToday);ui.selectionHint=node('p');
   ui.recordings=node('div');ui.recordingsEmpty=node('p','此日期范围暂无保留采集数据。');ui.recordings.append(ui.recordingsEmpty);
   ui.jobs=node('div');ui.jobsEmpty=node('p','尚无上传任务。');ui.jobs.append(ui.jobsEmpty);
   const clean=node('section',null,'pcu-card');ui.cleanupHint=node('p');const cleanActions=node('div',null,'pcu-actions');
   ui.selectCleanup=button('全选可清理批次',()=>{for(const item of data?.recordings||[])if(canCleanup(item))cleanupSelection.add(String(item.cloud.job_id));closeConfirmation();reconcile();});ui.cleanup=button('清理已选 0 批本地数据',cleanup,'pcu-danger');cleanActions.append(ui.selectCleanup,ui.cleanup);clean.append(node('b','上传完成后的本地清理'),ui.cleanupHint,cleanActions);
   root.append(title,intro,ui.network,ui.toolbar,account,target,overview,ui.confirmation,node('h3','选择采集批次'),ui.selectionHint,actions,ui.recordings,node('h3','上传状态'),ui.jobs,clean);controls();
  }
  function suspend() {
   version++;clearTimeout(timer);timer=null;loading=null;busy=false;root.hidden=true;
   if(ui.ak)ui.ak.value='';if(ui.sk)ui.sk.value='';root.replaceChildren();
   uploadSelection.clear();cleanupSelection.clear();recordingRows.clear();jobRows.clear();ui={};confirmation=null;
  }
  return {
   suspend,
   update(){if(!active()){clearTimeout(timer);timer=null;return;}controls();if(!loading&&!busy&&!timer)schedule(Math.max(0,2000-(Date.now()-lastRead)));},
   async open(){suspend();day='today';data=null;lastRead=0;editingCredentials=false;settingsLoaded=false;root.hidden=false;build();await load();}
  };
 };
})(window);
