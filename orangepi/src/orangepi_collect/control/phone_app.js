/* Phone companion for the existing dashboard. No framework, remote asset,
 * raw-data cache, recorder, or independent control state machine. */
(function (global) {
 'use strict';
 global.PI_REFERENCE_FIX_VERSION='20261105.2';
 global.PI_REFERENCE_CONNECTION_FIX='20261105.3';
 function createPhoneCompanion(options) {
  const $ = id => document.getElementById(id);
  const panel = document.createElement('section');
  panel.id = 'phone-panel'; panel.className = 'card';
  panel.innerHTML = `<div class="card-head"><div><h3>随身采集终端</h3><p>手机与 Pi 连接同一网络，即可查看和操作。</p></div></div>
   <div class="phone-body"><div class="phone-row"><button id="phone-tasks">任务列表</button><button id="phone-references">参考图库 · 上传/删除</button><button id="phone-history">历史回看</button><button id="phone-recordings">采集管理 · 云端上传/清理</button><button id="phone-audio">耳机设置</button></div>
   <div class="phone-row"><button id="phone-install">添加到手机桌面</button><button id="phone-awake">保持亮屏</button><a href="/references" target="_blank" rel="noopener">管理参考图库</a></div>
   <p class="phone-hint" id="phone-help">参考图可点击放大。录制前自动遮黑，长按黑屏三秒结束采集。图库管理使用独立管理码。</p>
   <section id="phone-reference-library" hidden></section><section id="phone-recording-manager" hidden></section><div class="phone-list" id="phone-list"></div><div class="phone-row" id="phone-pages" hidden><button id="phone-previous">上一页</button><button id="phone-next">下一页</button></div>
   <section id="phone-qc" hidden><div class="phone-qc-heading"><b id="phone-qc-summary"></b><button id="phone-qc-all">全部问题</button><button id="phone-qc-emg-left">左臂肌电</button><button id="phone-qc-emg-right">右臂肌电</button></div><div id="phone-qc-findings"></div><div class="phone-row" id="phone-qc-pages"><button id="phone-qc-previous">上一页问题</button><button id="phone-qc-next">下一页问题</button></div><small id="phone-qc-position"></small></section>
   <div id="phone-player" hidden><h3 id="phone-stream-title"></h3><p class="phone-hint" id="phone-replay-note"></p>
    <img id="phone-replay-image" hidden alt="已保存录像帧"><canvas id="phone-plot" width="900" height="420" hidden></canvas>
    <input id="phone-timeline" aria-label="回看位置" type="range" min="0" max="0" value="0" style="width:100%;margin:16px 0">
    <div class="phone-row"><button id="phone-back">上一帧</button><button id="phone-play">播放</button><button id="phone-forward">下一帧</button><button id="phone-speed">1×</button></div>
    <div class="phone-row" id="phone-signal-options" hidden><button id="phone-window">2 秒窗</button><button id="phone-filter">原始信号</button></div>
    <p id="phone-position" class="phone-hint"></p></div><p id="phone-message" class="phone-hint" role="status"></p></div>`;
  document.querySelector('main').append(panel);
  const viewer = document.createElement('div'); viewer.id = 'phone-viewer'; viewer.hidden = true;
  viewer.setAttribute('role', 'dialog'); viewer.setAttribute('aria-label', '参考图细节'); viewer.setAttribute('aria-modal', 'true');
  viewer.innerHTML = `<div class="phone-row"><button id="phone-close">返回</button><button id="phone-minus">缩小</button><button id="phone-plus">放大</button><button id="phone-reset">复位</button><button id="phone-reference-retry" hidden>重新加载原图</button></div><p id="phone-reference-history"></p><p id="phone-zoom-label">1× · 双指缩放、拖动查看原图细节</p><div id="phone-viewport"><img id="phone-detail" alt="参考图细节" draggable="false"><pre id="phone-text-detail" hidden></pre></div>`;
  document.body.append(viewer);
  let shell = null;
  let referenceLibrary = null;
  let recordingManager = null;
  let lastPreparationCancel = null;
  let epoch = 0, context = '', mode = '', playing = false, timer = null, zoomTimer = null;
  let session = null, stream = null, frame = 0, signalStart = 0, speed = 1, signalWindow = 2, filtered = false;
  let replayController=null;
  let qcOffset=0,qcNext=null,qcStream='',qcVersion=0;
  let offset = 0, nextOffset = null, zoom = 1, cx = 5000, cy = 5000, zoomVersion = 0, replayVersion = 0;
  let installPrompt = null, wakeLock = null, keepAwake = false, wakePending = false, networkQueue = Promise.resolve();
  try{keepAwake=sessionStorage.getItem('pi-keep-awake')==='1';}catch{}
  const pending = new Set(), urls = new Map(), pointers = new Map();
  const safe = () => !document.hidden && options.unlocked();
  const message = text => { $('phone-message').textContent = text; };
  const endpoint = path => '/api/phone/sidecar' + path;
  function button(label, callback, disabled = false) {
   const b = document.createElement('button'); b.textContent = label; b.disabled = disabled; b.onclick = callback; return b;
  }
  function clearImage(id) {
   const image = $(id); image.removeAttribute('src');
   if (urls.has(id)) { URL.revokeObjectURL(urls.get(id)); urls.delete(id); }
  }
  const referenceThumb=$('reference-image'),referenceLoad=document.createElement('p');
  referenceLoad.id='phone-reference-load';referenceLoad.className='phone-hint';referenceLoad.hidden=true;referenceLoad.setAttribute('role','status');
  const referenceLoadText=document.createElement('span'),referenceReload=document.createElement('button');referenceReload.textContent='重新加载参考图';referenceReload.style.marginLeft='8px';
  referenceLoad.append(referenceLoadText,referenceReload);referenceThumb.insertAdjacentElement('afterend',referenceLoad);
  let resumeReferenceKey='',referenceKey='',referenceTries=0,referenceTimer=null,referenceWatchdog=null,referenceLoaded=false,referenceBlob=null,referenceBlobKey='';
  const referenceIdentity=()=>{const s=options.status()||{};return s.image?[s.privacy?.instance,s.session,s.image].join('|'):'';};
  function stopReferenceTimers(){clearTimeout(referenceTimer);clearTimeout(referenceWatchdog);referenceTimer=referenceWatchdog=null;}
  function clearReferenceMemory(){stopReferenceTimers();referenceLoaded=false;referenceBlob=null;referenceBlobKey='';referenceLoad.hidden=true;}
  function referencePermitted(){const s=options.status()||{};return safe()&&!!s.image&&s.privacy?.required===false&&s.privacy?.stop_confirmed===true&&referenceKey===referenceIdentity();}
  function nativeReferenceRequest(){
   referenceTimer=null;if(!referencePermitted())return;
   referenceLoaded=false;referenceLoad.hidden=false;referenceLoadText.textContent='正在加载参考图…';referenceReload.disabled=true;
   const url=new URL('/api/reference',location.origin),headers=options.headers(),token=headers['X-Access-Token'];
   if(token)url.searchParams.set('token',token);url.searchParams.set('retry',String(Date.now()));
   referenceThumb.classList.remove('hidden');referenceThumb.src=url.href;watchReference();
  }
  function referenceFailure(){
   clearTimeout(referenceWatchdog);referenceWatchdog=null;if(!referencePermitted()||referenceLoaded||referenceTimer!==null)return;
   referenceLoad.hidden=false;referenceReload.disabled=false;
   if(referenceTries<3){
    const delay=[500,1500,3000][referenceTries++];referenceLoadText.textContent='参考图加载失败，正在重试（'+referenceTries+'/3）…';
    if(referenceTimer===null)referenceTimer=setTimeout(nativeReferenceRequest,delay);
   }else referenceLoadText.textContent='参考图暂未显示，请点重新加载。';
  }
  function watchReference(){clearTimeout(referenceWatchdog);referenceWatchdog=setTimeout(referenceFailure,8000);}
  referenceThumb.addEventListener('load',()=>{
   if(!referencePermitted()||!referenceThumb.naturalWidth)return;
   referenceLoaded=true;referenceTries=0;stopReferenceTimers();referenceLoad.hidden=true;referenceReload.disabled=false;
  });
  referenceThumb.addEventListener('error',referenceFailure);
  referenceReload.onclick=()=>{if(!referencePermitted())return;referenceTries=0;stopReferenceTimers();nativeReferenceRequest();};
  function syncReference(snapshot){
   const key=referenceIdentity();
   if(!safe()||snapshot.privacy?.required!==false||snapshot.privacy?.stop_confirmed!==true||!snapshot.image){clearReferenceMemory();referenceKey='';referenceLoaded=false;return;}
   if(referenceKey!==key){clearReferenceMemory();referenceKey=key;referenceTries=0;referenceLoaded=false;}
   if(snapshot.reference_image_state?.kind==='text'){stopReferenceTimers();referenceThumb.removeAttribute('src');referenceThumb.classList.add('hidden');referenceLoad.hidden=true;return;}
   referenceReload.disabled=false;
   if(referenceThumb.complete&&referenceThumb.naturalWidth){referenceLoaded=true;stopReferenceTimers();referenceLoad.hidden=true;return;}
   if(referenceLoaded)return;
   referenceLoad.hidden=false;
   if(referenceTimer!==null||referenceWatchdog!==null)return;
   if(referenceThumb.getAttribute('src')&&!referenceThumb.complete){referenceLoadText.textContent='正在加载参考图…';watchReference();}
   else if(referenceTries<3)referenceFailure();
  }
  async function decodeDisplayImage(image){
   const controller=new AbortController();pending.add(controller);let reason=null,rejectInterrupted;
   const interrupted=new Promise((_,reject)=>{rejectInterrupted=reject;});interrupted.catch(()=>{});
   const cancel=()=>rejectInterrupted(reason||new DOMException('图片显示已取消','AbortError'));
   controller.signal.addEventListener('abort',cancel,{once:true});
   const timer=setTimeout(()=>{reason=new DOMException('图片解码超时，请重新加载','TimeoutError');controller.abort();},5000);
   try{
    const decode=image.decode?image.decode():new Promise((resolve,reject)=>{image.onload=resolve;image.onerror=reject;});
    await Promise.race([decode,interrupted]);
   }finally{clearTimeout(timer);pending.delete(controller);controller.signal.removeEventListener('abort',cancel);}
  }
  function stopPlaying(release=true) {const wasPlaying=playing;playing=false;replayController?.abort();replayController=null;clearTimeout(timer);timer=null;$('phone-play').textContent='播放';if(wasPlaying&&release)releaseReplay();}
  function request(path, config = {}, allowLocked = false) {
   const identity=epoch,controller=new AbortController();pending.add(controller);
   const {timeoutMs=10000,...fetchConfig}=config;
   let reason=null,released=false,rejectInterrupted;
   const interrupted=new Promise((_,reject)=>{rejectInterrupted=reject;});interrupted.catch(()=>{});
   const cancel=()=>rejectInterrupted(reason||new DOMException('显示请求已取消','AbortError'));
   controller.signal.addEventListener('abort',cancel,{once:true});
   // Start the budget before queueing. Cancellation rejects even on browsers
   // where fetch or a body reader fails to settle after abort().
   const timeout=setTimeout(()=>{reason=new DOMException('读取超时，请重新加载','TimeoutError');controller.abort();},timeoutMs);
   const release=()=>{if(released)return;released=true;clearTimeout(timeout);pending.delete(controller);controller.signal.removeEventListener('abort',cancel);};
   const guarded=value=>Promise.race([value,interrupted]);
   const execute=async()=>{
    if(controller.signal.aborted)throw reason||new DOMException('显示请求已取消','AbortError');
    if(!allowLocked&&(identity!==epoch||!safe()))throw new DOMException('显示上下文已变化','AbortError');
    const response=await guarded(fetch(path,{...fetchConfig,headers:{...options.headers(),...config.headers},signal:controller.signal,cache:'no-store'}));
    if(controller.signal.aborted)throw reason||new DOMException('显示请求已取消','AbortError');
    if(!response.ok){const data=await guarded(response.json()).catch(error=>{if(controller.signal.aborted)throw reason||error;return {};});throw Error(data.message||data.error||'读取失败，请重试');}
    for(const method of ['json','blob','arrayBuffer','text']){
     const read=response[method].bind(response);response[method]=async()=>{try{return await guarded(read());}finally{release();}};
    }
    if(response.headers.get('Content-Length')==='0')release();return response;
   };
   const queued=networkQueue.catch(()=>{}).then(execute);
   const job=guarded(queued).catch(error=>{release();throw error;});
   networkQueue=job.catch(()=>{});
   return job;
  }
  function report(error) { if (safe() && error.name !== 'AbortError') { stopPlaying(); message(error.message); } }
  function releaseReplay() {
   // A bounded release request frees the existing decoder; no UI action waits.
   request(endpoint('/replay/release'), {}, true).then(response=>response.json()).catch(() => {});
  }
  function suspend() {
   resumeReferenceKey='';
   referenceLibrary?.suspend();recordingManager?.suspend();
   shell?.suspend();
   const hadReplay = mode === 'replay'; epoch++; zoomVersion++; replayVersion++;qcVersion++; stopPlaying(false); clearTimeout(zoomTimer);
   for (const controller of pending) controller.abort(); pending.clear(); pointers.clear();
   clearImage('phone-detail'); $('phone-text-detail').textContent='';$('phone-text-detail').hidden=true;clearImage('phone-replay-image'); viewer.hidden = true; $('phone-player').hidden = true;
   $('phone-list').replaceChildren(); $('phone-pages').hidden = true; mode = ''; session = stream = null;
   $('phone-qc').hidden=true;$('phone-qc-findings').replaceChildren();
   $('phone-plot').getContext('2d').clearRect(0,0,900,420);
   if (hadReplay) releaseReplay();
  }
  function pause() {
   // The cover hides all content. Preserve navigation, forms and list selection
   // through a short reconnection; cancel optional traffic and live decoders.
   const protection=options.protection?.()?.snapshot?.privacy;if(mode==='reference'&&protection?.phase==='idle'&&protection.required===false)resumeReferenceKey=referenceIdentity();else resumeReferenceKey='';
   stopPlaying();shell?.pause();clearReferenceMemory();clearImage('phone-detail');$('phone-text-detail').textContent='';$('phone-text-detail').hidden=true;viewer.hidden=true;if(mode==='reference'){mode='';zoomVersion++;}
   for(const controller of pending)controller.abort();pending.clear();
  }
  async function showImage(id, response, identity, versionValid) {
   const blob = await response.blob();
   if (identity !== epoch || !safe() || !versionValid()) return false;
   const image = $(id), url = URL.createObjectURL(blob), previous = urls.get(id);
   urls.set(id,url); image.src = url;
   if (previous) URL.revokeObjectURL(previous);
   try {await decodeDisplayImage(image);}
   catch (error) {
    if(identity===epoch){clearImage(id);if(id==='phone-detail'){referenceBlob=null;referenceBlobKey='';}if(safe())throw Error(error.name==='TimeoutError'?error.message:'画面解码失败，请重新加载');}return false;
   }
   if(id==='phone-detail'&&identity===epoch&&safe()&&versionValid()){referenceBlob=blob;referenceBlobKey=referenceIdentity();}
   if (identity !== epoch || !safe() || !versionValid()) return false;
   await new Promise(resolve=>{const deadline=setTimeout(resolve,200);requestAnimationFrame(()=>{clearTimeout(deadline);resolve();});});
   return identity === epoch && safe() && versionValid();
  }
  function begin(nextMode) { suspend(); mode = nextMode; panel.classList.remove('phone-playing-panel');shell?.showAux(nextMode);message('正在读取…'); $('phone-player').hidden = true; }
  async function tasks() {return references();}
  async function history(page = 0) {
   begin('replay'); offset = page; const identity = epoch;
   try {
    const data = await (await request(endpoint('/replay/sessions?offset=' + page + '&limit=12'))).json();
    if (identity !== epoch || !safe()) return;
    for (const item of data.sessions || []) {const state=item.failure_stage==='preparation'?'准备失败 · 未正式录制':item.failure_stage==='acquisition'?'采集失败 · '+item.qc:'QC '+item.qc;const entry=button(item.label + ' · ' + state, () => chooseSession(item.id));if(item.qc==='ERROR')entry.classList.add('phone-qc-error');else if(item.qc==='WARN')entry.classList.add('phone-qc-warn');$('phone-list').append(entry);}
    nextOffset = data.next_offset; $('phone-pages').hidden = false;
    $('phone-previous').disabled = offset <= 0; $('phone-next').disabled = nextOffset == null;
    message(data.sessions?.length ? '只读回看；逐帧播放，处理较慢时会放慢。' : '暂无可回看的已保存采集段。');
   } catch (error) { report(error); }
  }
  async function chooseSession(id) {
   stopPlaying(); const identity = ++epoch;
   try {
    const data = await (await request(endpoint('/replay/session?id=' + encodeURIComponent(id)))).json();
    if (identity !== epoch || !safe()) return; session = data;
    $('phone-list').replaceChildren(button('返回采集段列表', () => history(offset))); $('phone-pages').hidden = true;
    for (const item of data.streams || []) {
     const qc=item.qc||{},label=item.label+' · ERROR '+(qc.error_count||0)+' / WARN '+(qc.warn_count||0)+(item.available?'':' · 无可回看数据');
     const entry=button(label,()=>chooseStream(item));entry.classList.add(qc.level==='ERROR'?'phone-qc-error':qc.level==='WARN'?'phone-qc-warn':'phone-qc-neutral');entry.dataset.stream=item.id;$('phone-list').append(entry);
    }
    $('phone-qc').hidden=false;qcStream='';qcOffset=0;await loadQC();
    const impedance = data.impedance || {};
    message([...(data.warnings || []), impedance.summary || '', ...(impedance.channels || []).map(c => c.label + ': ' + (c.value_kohm ?? '缺值') + ' kΩ')].join('；'));
   } catch (error) { report(error); }
  }
  function renderQC(data){
   $('phone-qc-summary').textContent=data.unavailable?'本段未取得可读取的 QC 结果':(qcStream?({'emg_left':'左臂肌电','emg_right':'右臂肌电','eeg':'脑电','board_imu':'头环惯性'}[qcStream]||qcStream)+'：':(data.scope==='emg_only'?'肌电即时质检（其余待主机检查）：':'本段所有检查：'))+'ERROR '+(data.error_count||0)+' 条 / WARN '+(data.warn_count||0)+' 条';
   const container=$('phone-qc-findings');container.replaceChildren();
   for(const finding of data.findings||[]){
    const row=document.createElement('article');row.className='phone-qc-finding '+(finding.level==='ERROR'?'phone-qc-error':'phone-qc-warn');
    const head=document.createElement('b');head.textContent=finding.level+' · '+(finding.label||finding.modality||'会话')+' · '+(finding.title||(finding.check==='acquisition'?'采集进程':finding.check||'质量检查'));
    const message=document.createElement('p');message.textContent=finding.reason||finding.message;
    const detail=document.createElement('small');detail.textContent=[finding.field&&'字段 '+finding.field,Number.isFinite(finding.threshold)&&'阈值 '+finding.threshold,Number.isFinite(finding.observed)&&'实测 '+finding.observed,...(finding.spans||[]).slice(0,5).map(span=>(Number.isFinite(finding.offset_seconds)?'开录后 '+finding.offset_seconds.toFixed(2)+' 秒':'记录中的时间 '+span.t)+' / '+(span.dur??span.duration??0)+' 秒'+((span.msg||span.label)?' · '+(span.msg||span.label):''))].filter(Boolean).join('；');
    row.append(head,message);if(finding.action){const action=document.createElement('p');action.textContent='处理建议：'+finding.action;row.append(action);}row.append(detail);container.append(row);
   }
   if(!(data.findings||[]).length){const note=document.createElement('p');note.textContent=data.unavailable?'本段未保存可读取的 QC 报告。':'此筛选范围没有 WARN／ERROR 条目。';container.append(note);}
   qcNext=data.next_offset;$('phone-qc-previous').disabled=qcOffset===0;$('phone-qc-next').disabled=qcNext==null;
   $('phone-qc-pages').hidden=!(qcOffset||qcNext!=null);$('phone-qc-position').textContent='共 '+(data.total??data.total_findings??0)+' 条，显示 '+(data.findings?.length?qcOffset+1:0)+'–'+(qcOffset+(data.findings?.length||0))+' 条';
  }
  async function loadQC(offset=0,selected=qcStream){
   if(!session||!safe())return;const identity=epoch,version=++qcVersion;qcOffset=offset;qcStream=selected;
   try{
    const data=await(await request(endpoint('/replay/qc?id='+encodeURIComponent(session.id)+'&offset='+offset+'&limit=50&stream='+encodeURIComponent(selected)))).json();
    if(identity!==epoch||version!==qcVersion||!safe())return;renderQC(data);
   }catch(error){if(identity===epoch&&version===qcVersion&&safe()){
    $('phone-qc-summary').textContent='无法读取 QC 明细：'+error.message+'，请重新选择模态重试。';
    $('phone-qc-findings').replaceChildren();$('phone-qc-pages').hidden=true;$('phone-qc-position').textContent='';qcNext=null;
   }}
  }
  function drawSignal(data) {
   const canvas = $('phone-plot'), ctx = canvas.getContext('2d'), labels = data.channel_labels || [], rows = data.rows || [];
   ctx.clearRect(0,0,900,420); ctx.font = '17px sans-serif';
   labels.forEach((label, channel) => {
    const values = rows.flatMap(row => (row[1]?.[channel] || []).filter(Number.isFinite));
    let low = Infinity, high = -Infinity; for (const value of values) {low = Math.min(low,value); high = Math.max(high,value);}
    const center = (channel + .5) * 420 / labels.length, scale = 420 / labels.length * .36 / Math.max(high - low,1e-12), mid = (low + high) / 2;
    ctx.fillStyle = '#687d89'; ctx.fillText(label,8,center+5); ctx.strokeStyle = '#006e78'; ctx.beginPath();
    for (const row of rows) {
     const pair = row[1]?.[channel]; if (!pair || !pair.every(Number.isFinite)) continue;
     const x = 95 + (row[0] - data.start) / data.duration * 790;
     ctx.moveTo(x,center-(pair[0]-mid)*scale); ctx.lineTo(x,center-(pair[1]-mid)*scale);
    }
    ctx.stroke();
   });
  }
  async function displayFrame() {
   if (!stream || !session || !safe()) return;
   const identity = epoch, version = ++replayVersion, selected = stream;
   const valid = () => version === replayVersion && stream === selected;
   const started = performance.now();
   try {
    const query = 'session=' + encodeURIComponent(session.id) + '&stream=' + encodeURIComponent(stream.id);
    if (stream.kind === 'video') {
     const response = await request(endpoint('/replay/frame.jpg?' + query + '&frame=' + frame));
     if (!await showImage('phone-replay-image',response,identity,valid)) return;
     $('phone-position').textContent = (frame+1) + ' / ' + stream.frames + ' 帧 · ' + (frame / (stream.fps || 1)).toFixed(2) + ' 秒';
     $('phone-timeline').value = frame;
     if (playing && frame + 1 < stream.frames) {
      // Advance by exactly one only after decode and presentation complete.
      frame++;
      timer = setTimeout(displayFrame,Math.max(0,1000 / Math.max(stream.fps || 1,1) / speed - (performance.now()-started)));
     } else stopPlaying();
    } else {
     const data = await (await request(endpoint('/replay/signal?' + query + '&start=' + signalStart + '&duration=' + signalWindow + '&width=400&mode=' + (filtered?'filtered':'raw')))).json();
     if (identity !== epoch || !safe() || !valid()) return;
     drawSignal(data); $('phone-timeline').value = signalStart;
     $('phone-position').textContent = signalStart.toFixed(2) + ' 秒 · ' + (data.warnings || []).join('；');
    }
   } catch (error) { report(error); }
  }
  async function playStream(){
   const identity=epoch,version=replayVersion;replayController=new AbortController();const controller=replayController;
   try{while(playing&&safe()&&identity===epoch&&version===replayVersion&&frame<stream.frames){
    const beginFrame=frame;
    await global.readPhoneFrames('/api/phone/replay-stream?session='+encodeURIComponent(session.id)+'&stream='+encodeURIComponent(stream.id)+'&start='+frame+'&limit=120',{headers:options.headers(),signal:controller.signal,onFrame:async(meta,blob)=>{
     if(!playing||identity!==epoch||version!==replayVersion||!safe()){controller.abort();return;}
     if(meta.frame!==frame)throw Error('回看帧序号不连续，已暂停');const started=performance.now();
     if(!await showImage('phone-replay-image',{blob:async()=>blob},identity,()=>version===replayVersion&&playing))return;
     $('phone-position').textContent=(frame+1)+' / '+stream.frames+' 帧 · '+(frame/(stream.fps||1)).toFixed(2)+' 秒';$('phone-timeline').value=frame;frame++;
     const delay=1000/Math.max(stream.fps||1,1)/speed-(performance.now()-started);if(delay>0)await new Promise(resolve=>setTimeout(resolve,delay));
    }});
    if(frame===beginFrame)throw Error('回看暂未返回新画面');
   }if(identity===epoch&&version===replayVersion&&playing){frame=Math.max(0,stream.frames-1);stopPlaying();}}
   catch(error){if(identity===epoch&&version===replayVersion&&playing&&error.name!=='AbortError')report(error);}
   finally{if(replayController===controller)replayController=null;}
  }
  function chooseStream(item) {
   panel.classList.add('phone-playing-panel');
   stopPlaying(); replayVersion++; clearImage('phone-replay-image'); stream = item; frame = signalStart = 0; filtered = false;
   $('phone-player').hidden = false; const video = item.kind === 'video';
   for(const id of ['phone-play','phone-back','phone-forward','phone-timeline'])$(id).disabled=!item.available;
   $('phone-replay-image').hidden = !video; $('phone-plot').hidden = video; $('phone-play').hidden = !video; $('phone-speed').hidden = !video;
   $('phone-signal-options').hidden = video; $('phone-filter').disabled = item.id !== 'eeg'; $('phone-filter').textContent = '原始信号';
   $('phone-back').textContent = video?'上一帧':'前一窗'; $('phone-forward').textContent = video?'下一帧':'后一窗';
   $('phone-stream-title').textContent = item.label;
   loadQC(0,item.id);
   $('phone-replay-note').textContent = video?'派生缩略图，原始保存文件不改动。':'min/max 包络保留极值，原始保存文件不改动。';
   $('phone-timeline').max = video?Math.max(0,item.frames-1):item.duration_seconds || 0; $('phone-timeline').step = video?'1':'.01';
   if(item.available)displayFrame();else{$('phone-replay-image').hidden=true;$('phone-plot').hidden=true;$('phone-position').textContent='该模态没有可回看的落盘内容，请检查上方 QC 原因。';}
  }
  let panX=0,panY=0,refWidth=0,refHeight=0,refFrame=null,gesture=null;
  const viewport=$('phone-viewport');
  function fitSize(){const bounds=viewport.getBoundingClientRect(),scale=refWidth&&refHeight?Math.min(bounds.width/refWidth,bounds.height/refHeight):0;return {width:refWidth*scale,height:refHeight*scale,bounds};}
  function paintReference(){
   refFrame=null;if(viewer.hidden||!safe()||!refWidth)return;
   const fit=fitSize(),limitX=Math.max(0,(fit.width*zoom-fit.bounds.width)/2),limitY=Math.max(0,(fit.height*zoom-fit.bounds.height)/2);
   panX=Math.max(-limitX,Math.min(limitX,panX));panY=Math.max(-limitY,Math.min(limitY,panY));
   const image=$('phone-detail');image.style.width=fit.width+'px';image.style.height=fit.height+'px';
   image.style.transform='translate(-50%,-50%) translate('+panX+'px,'+panY+'px) scale('+zoom+')';
   $('phone-zoom-label').textContent=Number(zoom.toFixed(2))+'× · 双指连续缩放，单指拖动';
  }
  function requestPaint(){if(!$('phone-text-detail').hidden)return;if(refFrame===null)refFrame=requestAnimationFrame(paintReference);}
  async function reference(reload=false){
   if(!safe()||!options.status()?.image)return;
   const force=reload===true,key=referenceIdentity();if(force){referenceBlob=null;referenceBlobKey='';}
   begin('reference');zoom=1;panX=panY=refWidth=refHeight=0;viewer.hidden=false;$('phone-close').focus();$('phone-zoom-label').textContent='正在载入原图…';$('phone-reference-retry').hidden=true;
   const identity=epoch,version=++zoomVersion,isText=options.status()?.reference_image_state?.kind==='text';
   viewport.classList.toggle('text-mode',isText);$('phone-detail').hidden=isText;$('phone-text-detail').hidden=!isText;$('phone-text-detail').textContent='';
   for(const id of ['phone-minus','phone-plus','phone-reset'])$(id).hidden=isText;
   try{
    if(isText){
     const response=await request('/api/reference');if(!response.ok)throw Error('任务说明暂不可用');
     const value=await response.json();if(identity!==epoch||viewer.hidden||!safe()||version!==zoomVersion||key!==referenceIdentity())return;
     if(value.kind!=='text'||typeof value.reference_text!=='string')throw Error('任务说明格式无效');
     $('phone-text-detail').textContent=[typeof value.name==='string'?value.name.trim():'',value.reference_text].filter(Boolean).join('\n\n');$('phone-zoom-label').textContent='任务说明';return;
    }
    let response;
    if(referenceBlob&&referenceBlobKey===key)response=new Response(referenceBlob,{headers:{'Content-Type':referenceBlob.type||'image/png'}});
    else response=await request('/api/reference');
    if(!await showImage('phone-detail',response,identity,()=>zoomVersion===version))return;
    refWidth=$('phone-detail').naturalWidth;refHeight=$('phone-detail').naturalHeight;paintReference();
   }catch(error){
    if(identity===epoch&&!viewer.hidden&&safe()){$('phone-zoom-label').textContent=error.name==='AbortError'?'已取消加载，请重新加载。':error.message;$('phone-reference-retry').hidden=false;}
   }
  }
  function changeZoom(value){
   if(!safe()||viewer.hidden)return;const next=Math.max(1,Math.min(16,value)),ratio=next/zoom;panX*=ratio;panY*=ratio;zoom=next;requestPaint();
  }
  function rebase(){
   const points=[...pointers.values()];if(!points.length){gesture=null;return;}
   gesture={zoom,panX,panY,first:points[0],distance:points.length>1?Math.hypot(points[1].x-points[0].x,points[1].y-points[0].y):0,
    midpoint:points.length>1?{x:(points[0].x+points[1].x)/2,y:(points[0].y+points[1].y)/2}:points[0]};
  }
  viewport.addEventListener('pointerdown',event=>{
   if(!safe()||viewer.hidden||!$('phone-text-detail').hidden)return;event.preventDefault();viewport.setPointerCapture(event.pointerId);pointers.set(event.pointerId,{x:event.clientX,y:event.clientY});rebase();
  });
  viewport.addEventListener('pointermove',event=>{
   if(!gesture||!pointers.has(event.pointerId)||!safe()||!$('phone-text-detail').hidden)return;event.preventDefault();pointers.set(event.pointerId,{x:event.clientX,y:event.clientY});const points=[...pointers.values()];
   if(points.length>1&&gesture.distance>0){
    const bounds=viewport.getBoundingClientRect(),mid={x:(points[0].x+points[1].x)/2,y:(points[0].y+points[1].y)/2};
    zoom=Math.max(1,Math.min(16,gesture.zoom*Math.hypot(points[1].x-points[0].x,points[1].y-points[0].y)/gesture.distance));
    const ratio=zoom/gesture.zoom,originX=gesture.midpoint.x-bounds.left-bounds.width/2,originY=gesture.midpoint.y-bounds.top-bounds.height/2;
    panX=mid.x-bounds.left-bounds.width/2-(originX-gesture.panX)*ratio;panY=mid.y-bounds.top-bounds.height/2-(originY-gesture.panY)*ratio;
   }else{panX=gesture.panX+points[0].x-gesture.first.x;panY=gesture.panY+points[0].y-gesture.first.y;}
   requestPaint();
  });
  function endPointer(event){if(!$('phone-text-detail').hidden)return;pointers.delete(event.pointerId);rebase();requestPaint();}
  viewport.addEventListener('pointerup',endPointer);viewport.addEventListener('pointercancel',endPointer);
  viewport.addEventListener('wheel',event=>{if(!safe()||viewer.hidden||!$('phone-text-detail').hidden)return;event.preventDefault();changeZoom(zoom*Math.exp(-event.deltaY*.002));},{passive:false});
  if(global.ResizeObserver)new ResizeObserver(()=>requestPaint()).observe(viewport);
  async function audio(action, address) {
   if(shell?.openHeadphones){shell.openHeadphones();return;}
   if (mode!=='audio') begin('audio'); const identity=epoch;
   try {
    const config=action?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(address?{action,address}:{action})}:{};
    const data=await (await request(endpoint('/headphones'),config)).json(); if (identity!==epoch || !safe()) return;
    $('phone-list').replaceChildren(button('刷新耳机状态',() => audio()));
    for (const item of data.devices || []) $('phone-list').append(button((item.name || item.address)+(item.connected?' · 已连接':''),() => audio('connect',item.address),data.busy));
    $('phone-list').append(button('搜索耳机',() => audio('scan'),data.busy),button('试听',() => audio('test'),data.busy),button('断开耳机',() => audio('disconnect'),data.busy));
    message([data.status,data.error,data.pairing_progress,data.test_result].filter(Boolean).join('；'));
   } catch (error) { report(error); }
  }
  $('reference-image').onclick=reference;
  $('reference-text-button').onclick=reference;
  $('phone-reference-retry').onclick=()=>reference(true);
  $('phone-close').onclick=() => {suspend();$('reference-image').focus();};
  $('phone-minus').onclick=() => changeZoom(zoom/2); $('phone-plus').onclick=() => changeZoom(zoom*2);
  $('phone-reset').onclick=() => {panX=panY=0;changeZoom(1);};
  $('phone-tasks').onclick=() => tasks(); $('phone-history').onclick=() => history(); $('phone-audio').onclick=() => audio();
  $('phone-previous').onclick=() => mode==='tasks'?tasks(Math.max(0,offset-12)):history(Math.max(0,offset-12));
  $('phone-next').onclick=() => {if(nextOffset!=null)(mode==='tasks'?tasks:history)(nextOffset);};
  $('phone-play').onclick=() => {if(playing)stopPlaying();else if(safe() && stream){replayVersion++;playing=true;$('phone-play').textContent='暂停';playStream();}};
  function seek(value) {stopPlaying();replayVersion++;if(!stream)return;if(stream.kind==='video')frame=Math.max(0,Math.min(stream.frames-1,Math.round(value)));else signalStart=Math.max(0,Math.min(stream.duration_seconds||0,value));displayFrame();}
  $('phone-back').onclick=() => seek(stream?.kind==='video'?frame-1:signalStart-signalWindow);
  $('phone-forward').onclick=() => seek(stream?.kind==='video'?frame+1:signalStart+signalWindow);
  $('phone-timeline').onchange=event => seek(Number(event.target.value));
  $('phone-speed').onclick=() => {speed=speed===1?2:speed===2?.5:1;$('phone-speed').textContent=speed+'×';};
  $('phone-window').onclick=() => {signalWindow=signalWindow===2?10:signalWindow===10?.5:2;$('phone-window').textContent=signalWindow+' 秒窗';displayFrame();};
  $('phone-filter').onclick=() => {filtered=!filtered;$('phone-filter').textContent=filtered?'滤波信号':'原始信号';displayFrame();};
  $('phone-qc-emg-left').onclick=()=>{stopPlaying();loadQC(0,'emg_left');};$('phone-qc-emg-right').onclick=()=>{stopPlaying();loadQC(0,'emg_right');};
  $('phone-qc-all').onclick=()=>{stopPlaying();loadQC(0,'');};$('phone-qc-previous').onclick=()=>{stopPlaying();loadQC(Math.max(0,qcOffset-50));};$('phone-qc-next').onclick=()=>{if(qcNext!=null){stopPlaying();loadQC(qcNext);}};
  global.addEventListener('beforeinstallprompt',event => {event.preventDefault();installPrompt=event;});
  $('phone-install').onclick=async () => {
   if(installPrompt){await installPrompt.prompt();installPrompt=null;}
   else message('iPhone：Safari 分享 → 添加到主屏幕。Android：浏览器菜单 → 添加到主屏幕。局域网 HTTP 地址可能只支持快捷方式；完整安装需要受信任的 HTTPS。');
  };
  let wakeGeneration=0;
  async function acquireWakeLock() {
   if(!keepAwake || document.hidden || !safe() || wakeLock || wakePending)return;
   const generation=wakeGeneration;wakePending=true;
   try {
    const acquired=await navigator.wakeLock.request('screen');
    if(!keepAwake || generation!==wakeGeneration || document.hidden || !safe()){if(!acquired.released)await acquired.release();return;}
    if(acquired.released)return;
    wakeLock=acquired;acquired.addEventListener('release',() => {if(wakeLock===acquired)wakeLock=null;});
    message('已请求保持亮屏；系统省电策略仍可能锁屏。');
   }
   catch {if(keepAwake && generation===wakeGeneration)message('此浏览器或当前连接不支持保持亮屏，请在手机系统中设置自动锁屏时间。');}
   finally{wakePending=false;}
  }
  $('phone-awake').onclick=async () => {keepAwake=!keepAwake;try{sessionStorage.setItem('pi-keep-awake',keepAwake?'1':'0');}catch{}if(!keepAwake){wakeGeneration++;const previous=wakeLock;wakeLock=null;$('phone-awake').textContent='保持亮屏';if(previous&&!previous.released)await previous.release();}else{$('phone-awake').textContent='允许息屏';await acquireWakeLock();}};
  document.addEventListener('visibilitychange',() => {if(document.hidden)pause();else acquireWakeLock();});
  document.addEventListener('keydown',event => {if(event.key==='Escape'&&!viewer.hidden)suspend();});
  async function references(){begin('references');await referenceLibrary?.open();}
  referenceLibrary=global.createPhoneReferenceLibrary?.({root:$('phone-reference-library'),request,status:options.status,action:options.action,message,current:()=>mode==='references'});
  $('phone-references').onclick=references;
  async function recordings(){begin('recordings');await recordingManager?.open();}
  recordingManager=global.createPhoneRecordingManager?.({root:$('phone-recording-manager'),request,status:options.status,action:options.action,refresh:options.refresh,message,current:()=>mode==='recordings'});
  $('phone-recordings').onclick=recordings;
  shell=global.createPhoneShell?.({panel,options,openHistory:()=>history(),openTasks:()=>tasks(),openAudio:()=>audio(),closeAux:suspend,openReference:reference,openRecordings:recordings});
  return {suspend,pause,displayOptions:()=>Object.assign({},shell?.displayOptions()||{review:true,cameras:true},{referenceOnly:mode==='reference'||shell?.referenceViewing?.()===true}),beforeStart:()=>shell?.beforeStart()!==false,managesPreview:!!shell,legacyFrameVisible:name=>shell?.allowLegacyFrame(name),update(snapshot) {
   if(snapshot.preparation_cancelled_at&&snapshot.preparation_cancelled_at!==lastPreparationCancel){lastPreparationCancel=snapshot.preparation_cancelled_at;suspend();}
   const value=[snapshot.privacy?.instance,snapshot.session,snapshot.image,snapshot.preview_id].join('|');
    const keepManager=['recordings','references','tasks'].includes(mode)&&context.split('|')[0]===snapshot.privacy?.instance&&!snapshot.privacy?.required&&snapshot.privacy?.stop_confirmed&&['device_confirm','idle','failed_wait','exhausted','scanning','usb_importing','usb_import_recovery'].includes(snapshot.state);
   const keepReference=(mode==='reference'||resumeReferenceKey===referenceIdentity())&&context.split('|').slice(0,3).join('|')===value.split('|').slice(0,3).join('|');
   if(context && context!==value&&!keepManager&&!keepReference)suspend(); context=value;
   shell?.update(snapshot);
   if(keepAwake&&!wakeLock&&!wakePending&&safe())acquireWakeLock();
   referenceLibrary?.update(snapshot);recordingManager?.update(snapshot);$('phone-reference-history').textContent=global.describeReferenceState?.(snapshot.reference_image_state)||'';
   syncReference(snapshot);
   if(!safe()){if(mode)suspend();return;}
   if(resumeReferenceKey){const wanted=resumeReferenceKey;resumeReferenceKey='';if(wanted===referenceIdentity()&&snapshot.state==='ready'&&!mode)reference();}
   if(!mode&&referenceLibrary?.resumeNeeded()&&(snapshot.actions||[]).includes('reference_list'))references();
   const canReplay=['device_confirm','idle','failed_wait','exhausted','confirm_stop'].includes(snapshot.state);
    $('phone-history').disabled=!canReplay; $('phone-tasks').disabled=!(snapshot.actions||[]).includes('reference_list')&&!['usb_importing','usb_import_recovery'].includes(snapshot.state);
   $('phone-references').disabled=!canReplay||!safe();$('phone-recordings').disabled=!canReplay||!safe();
  }};
 }
 global.createPhoneCompanion=createPhoneCompanion;
})(window);
