/* Reference management uses the existing durable catalog and collection owner. */
(function(global){
 'use strict';
 global.createPhoneReferenceLibrary=function({root,request,status,action,message,current}){
  let generation=0,key='',revision=0,selected='pool',list=null,trash=false,hiddenView=false,next=null,busy=false,loaded=false;
  let pickerTarget=null,pendingSelection=null,resumeOffered=false;
  let lastTransfer=null;
   let pendingBundles=null;
   let usbData=null,usbSelected=new Set(),usbRemove=new Set(),usbState={state:'idle'},usbPolling=false,usbPollAt=0,usbCompleteSeen='',usbSubmitting=false,usbUnknown=false,usbStatusKnown=false,usbSubmissionStamp='';
  const singleList=()=>status().task_list?.selection_scope==='single_list';
  const manualTasks=()=>status().task_list?.manual_task_switch===true;
  const activeTask=()=>manualTasks()?status().task_list?.block_task_id:status().task_list?.id;
  const switchAllowed=id=>manualTasks()&&(status().task_list?.allowed_task_ids||[]).includes(id);
  const urls=new Set();
  const el=(tag,text,className)=>{const node=document.createElement(tag);if(text)node.textContent=text;if(className)node.className=className;return node;};
  const row=()=>el('div','','phone-reference-row');
  const valid=id=>id===generation&&current();
  const query=values=>new URLSearchParams(values).toString();
  const headers=extra=>({'X-Reference-Key':key,...extra});
  function button(label,fn,classes=''){const node=el('button',label,classes);node.type='button';node.onclick=fn;return node;}
  function release(){for(const url of urls)URL.revokeObjectURL(url);urls.clear();}
  function suspend(){if(lastTransfer?.state==='running')lastTransfer.state='interrupted';generation++;key='';loaded=false;list=null;release();root.hidden=true;root.replaceChildren();}
  function lock(value){busy=value;for(const node of root.querySelectorAll('button,input,select'))node.disabled=value;controls();}
   let select,uploadLabel,bundleLabel,use,refresh,trashButton,hiddenButton,sampling,newName,createButton,items,count,more,preview,activeList,selectionBox,transfer,transferText,progress,usbScan,usbPanel,usbChoices,usbOldChoices,usbApply,usbInfo,usbProgress;
   const usbServerActive=()=>['building','ready','committing','needs_recovery'].includes(usbState.state)||['usb_importing','usb_import_recovery'].includes(status().state);
   const usbActive=()=>!usbStatusKnown||usbSubmitting||usbUnknown||usbServerActive();
  // Keep the native input connected while a mobile gallery is open. Privacy
  // suspension still removes every displayed image and cancels all requests.
  const files=el('input');files.type='file';files.accept='image/png,image/jpeg,image/webp';files.multiple=true;files.id='phone-reference-files';files.className='phone-reference-file';files.tabIndex=-1;files.setAttribute('aria-label','选择参考图片');root.parentElement.append(files);
  const bundles=el('input');bundles.type='file';bundles.accept='.zip,application/zip,application/x-zip-compressed';bundles.multiple=true;bundles.id='phone-reference-bundles';bundles.className='phone-reference-file';bundles.tabIndex=-1;bundles.setAttribute('aria-label','选择ZIP任务包');root.parentElement.append(bundles);
  bundles.onchange=async event=>{const chosen=Array.from(event.target.files||[]);event.target.value='';if(!chosen.length)return;pendingSelection=null;pendingBundles={files:chosen,plans:null};resumeOffered=false;if(current()&&loaded)await planBundles();};
  files.addEventListener('click',()=>{pickerTarget=selected;});
  files.onchange=async event=>{
   const chosen=Array.from(event.target.files||[]);event.target.value='';const target=pickerTarget||selected;pickerTarget=null;
   if(!chosen.length)return;
   pendingBundles=null;
   if(current()&&loaded&&target===selected){await uploadFiles(chosen);return;}
   pendingSelection={files:chosen,listId:target};resumeOffered=false;
  };
   function controls(){if(!use)return;const editable=!busy&&loaded&&!!key&&!(usbSubmitting||usbUnknown||usbServerActive());
   use.disabled=!editable||trash||hiddenView||(manualTasks()&&(!switchAllowed(selected)||!list||list.remaining===0||list.sampling_enabled===false))||(singleList()&&(!list||list.import_pending||['pool','all'].includes(selected)))||!(status().actions||[]).includes('reference_list');
   for(const node of root.querySelectorAll('[data-switch-task]')){const id=node.dataset.switchTask,current=id===activeTask();node.textContent=current?'当前列表':'切换到此列表';node.disabled=!editable||!switchAllowed(id)||current||node.dataset.unavailable==='true'||!(status().actions||[]).includes('reference_list');}
   files.disabled=!editable||trash||hiddenView;uploadLabel.classList.toggle('disabled',files.disabled);createButton.disabled=!editable||trash||hiddenView;newName.disabled=!editable||trash||hiddenView;more.hidden=next===null;
    bundles.disabled=!editable||trash||hiddenView;bundleLabel.classList.toggle('disabled',bundles.disabled);
    usbScan.disabled=!editable||!usbStatusKnown||!(status().actions||[]).includes('usb_task_import');
    usbApply.disabled=usbScan.disabled||!usbData||(!usbSelected.size&&!usbRemove.size);
    for(const node of usbPanel.querySelectorAll('input'))node.disabled=!editable;
   uploadLabel.parentElement.hidden=trash||hiddenView;
    for(const node of root.querySelectorAll('.phone-reference-item button,.phone-reference-selection button'))if(node!==usbApply)node.disabled=!editable;
   for(const node of sampling?.querySelectorAll('input[type=checkbox],input[type=radio]')||[])node.disabled=!editable||hiddenView||node.dataset.pending==='true'||(singleList()&&!(status().actions||[]).includes('reference_list'));
  }
  async function json(path,body){return (await request(path,{headers:headers(body?{'Content-Type':'application/json'}:{}),...(body?{method:'POST',body:JSON.stringify(body)}:{})})).json();}
  function errorMessage(error){return error.name==='AbortError'?'请求超时，请刷新确认操作结果。':error.name==='TypeError'?'网络连接中断，请恢复后刷新核对。':error.message;}
  function failure(error,id){if(valid(id))message(errorMessage(error));}
  function ui(){
   root.replaceChildren();root.hidden=false;
   const label=el('label','参考图列表');select=el('select');select.id='phone-reference-select';select.setAttribute('aria-label','参考图列表');label.append(select);
   refresh=button('刷新',()=>key?load(selected):open());trashButton=button('回收站',()=>{trash=!trash;hiddenView=false;load('all');});hiddenButton=button('隐藏列表',()=>{hiddenView=!hiddenView;trash=false;load('pool');});hiddenButton.id='phone-reference-hidden';const toolbar=row();toolbar.append(refresh,hiddenButton,trashButton);
   use=button(manualTasks()?'切换到此列表':singleList()?'用此列表采集':'启用混合抽取',()=>{if(!use.disabled)action('reference_list',{list_id:(singleList()||manualTasks())?selected:'pool',catalog_revision:revision});},'primary');use.id='phone-reference-use';
   count=el('p','','phone-hint');count.id='phone-reference-count';
   activeList=el('p','','phone-hint');activeList.id='phone-reference-active-list';
   sampling=el('section','','phone-reference-sampling');sampling.id='phone-reference-sampling';
   const upload=row();uploadLabel=el('label','上传图片','phone-reference-upload');uploadLabel.htmlFor='phone-reference-files';
    bundleLabel=el('label','从 ZIP 导入','phone-reference-upload');bundleLabel.htmlFor='phone-reference-bundles';bundleLabel.id='phone-reference-bundle-entry';upload.append(uploadLabel,bundleLabel,use);
    usbScan=button('从 U 盘选择 ZIP',scanUsb);usbScan.id='phone-reference-usb-scan';upload.insertBefore(usbScan,use);
    usbPanel=el('section','','phone-reference-selection');usbPanel.id='phone-reference-usb-panel';
    usbInfo=el('p','插入U盘后扫描，勾选需要的 ZIP；不会默认全选。','phone-hint');usbInfo.setAttribute('role','status');usbInfo.setAttribute('aria-live','polite');
    usbChoices=el('div');usbOldChoices=el('div');usbApply=button('应用所选导入和删除',applyUsb,'primary');usbApply.id='phone-reference-usb-apply';
    usbProgress=el('progress');usbProgress.hidden=true;usbProgress.setAttribute('aria-label','U盘任务包导入进度');
    usbPanel.append(usbInfo,el('b','从U盘添加任务包'),usbChoices,el('b','旧任务列表（默认保留）'),usbOldChoices,usbProgress,usbApply,el('small','勾选的旧列表会移入回收站，可在回收站恢复。未勾选旧列表、历史录制和本机标定保持不变。'));
   const create=row();newName=el('input');newName.placeholder='新列表名称';newName.maxLength=128;newName.setAttribute('aria-label','新列表名称');newName.id='phone-reference-name';
   createButton=button('新建列表',async()=>{if(busy||!loaded)return;const name=newName.value.trim();if(!name){message('请输入新列表名称。');newName.focus();return;}await mutate({action:'create_list',name},true);});create.append(newName,createButton);
   selectionBox=el('section','','phone-reference-selection');selectionBox.id='phone-reference-selection';selectionBox.hidden=true;
   transfer=el('section','','phone-reference-transfer');transfer.id='phone-reference-transfer';transfer.hidden=true;transfer.setAttribute('role','status');transfer.setAttribute('aria-live','polite');
   transferText=el('p');progress=el('progress');progress.id='phone-reference-progress';transfer.append(transferText,progress);
   if(lastTransfer&&['interrupted','paused'].includes(lastTransfer.state)){transfer.hidden=false;progress.max=lastTransfer.total;progress.value=lastTransfer.confirmed;transferText.textContent='上次上传已暂停，已确认 '+lastTransfer.confirmed+' / '+lastTransfer.total+' 张。未确认的图片请先刷新核对。';}
   items=el('div','','phone-reference-items');items.id='phone-reference-items';more=button('加载更多',()=>loadItems(false));
   preview=el('div','','phone-reference-preview');preview.hidden=true;preview.setAttribute('role','dialog');preview.setAttribute('aria-modal','true');preview.setAttribute('aria-label','参考图预览');
   const close=button('关闭预览',()=>{preview.hidden=true;preview.querySelector('img')?.removeAttribute('src');});const image=el('img');image.alt='参考图预览';preview.append(close,image);
   const advanced=el('details');advanced.append(el('summary','更多管理：新建列表与回收站'),create,trashButton);
    root.append(toolbar,activeList,upload,usbPanel,selectionBox,transfer,el('p',manualTasks()?'手动切换不会清除已完成进度；不切换就持续抽当前列表，做完后自动进入下一个，直到本机场景完成。':singleList()?'选定一个列表后，仅在该列表内随机抽取未完成条目；完成后手动切换列表，重采保持同一张。':'勾选的列表合并抽取，优先未采集任务；标签随任务保存。导入完整的新列表会自动加入任务池；隐藏列表只供查看。','phone-hint'),sampling,label,count,advanced,items,more,preview);showPendingSelection();update(status());
   select.onchange=()=>{selected=select.value;loadItems(true);};
  }
  async function load(wanted=selected){
   if(busy||!current())return;const id=generation;invalidate();lock(true);message('正在读取参考图库…');
   try{await catalog(wanted,id);if(valid(id))message(hiddenView?'隐藏列表不参与抽取；恢复后可重新选择。':manualTasks()?'可手动更换列表；不更换则持续当前列表，完成后自动进入下一列表。':singleList()?'仅在选定列表内随机抽取；完成后手动切换。':'多个启用列表一起随机抽取，重采保持同一张。');}
   catch(error){if(valid(id))invalidate('列表未能加载，请刷新后再操作。');failure(error,id);}finally{if(valid(id))lock(false);}
  }
  async function catalog(wanted,id){
   let rows=[],offset=0,version=null;
   do{const data=await json('/api/reference-library?'+query({offset,limit:50,include_deleted:trash?1:0,hidden_only:hiddenView?1:0}));if(!valid(id))return;
    if(version!==null&&version!==data.revision)throw Error('图库已更新，请点击刷新。');version=data.revision;rows.push(...data.lists);offset=data.next_offset;
   }while(offset!==null);
   revision=version;selected=rows.some(item=>item.id===wanted)?wanted:(rows[0]?.id||'');select.replaceChildren();sampling.replaceChildren();
   if(!trash){sampling.append(el('b',hiddenView?'隐藏区 · 不参与抽取':manualTasks()?'选择当前任务列表（可手动切换）':singleList()?'选择一个列表采集':'参与混合抽取的列表'));
    for(const entry of rows.filter(item=>!['pool','all'].includes(item.id))){const card=el('div','','phone-pool-row'),label=el('label'),check=el('input');check.type=singleList()?'radio':'checkbox';if(singleList()){check.name='phone-active-task-list';check.dataset.listId=entry.id;}check.checked=singleList()?status().task_list?.id===entry.id:entry.sampling_enabled&&!entry.hidden;check.disabled=hiddenView||entry.import_pending;check.setAttribute('aria-label',(singleList()?'用此列表采集：':'参与抽取：')+entry.name);
     check.dataset.pending=String(!!entry.import_pending);check.onchange=()=>singleList()?action('reference_list',{list_id:entry.id,catalog_revision:revision}):mutate({action:'set_list_sampling',id:entry.id,hidden:false,enabled:check.checked,tags:entry.tags||[]});label.append(check,document.createTextNode(entry.name+' · 剩余 '+entry.remaining+' / '+entry.count));card.append(label);
     const extra=el('details');extra.append(el('summary','标签与列表管理'));const tags=el('input');tags.value=(entry.tags||[]).join('，');tags.placeholder='附加标签，以逗号分隔';tags.setAttribute('aria-label','标签：'+entry.name);
     extra.append(el('small','列表名称会自动作为标签保存。'),tags,button('保存标签',()=>mutate({action:'set_list_sampling',id:entry.id,hidden:entry.hidden,enabled:entry.sampling_enabled,tags:tags.value.split(/[,，]/).map(s=>s.trim()).filter(Boolean)})),button(hiddenView?'恢复到可见列表':'移到隐藏区',()=>mutate({action:'set_list_sampling',id:entry.id,hidden:!hiddenView,enabled:false,tags:entry.tags||[]})));
     if(switchAllowed(entry.id)){const change=button(entry.id===activeTask()?'当前列表':'切换到此列表',()=>{if(!change.disabled){selected=entry.id;select.value=selected;loadItems(true);action('reference_list',{list_id:entry.id,catalog_revision:revision});}});change.dataset.switchTask=entry.id;change.dataset.unavailable=String(entry.remaining===0||entry.import_pending||entry.sampling_enabled===false);card.append(change);}
     card.append(button('查看任务',()=>{select.value=entry.id;selected=entry.id;loadItems(true);}),extra);sampling.append(card);
    }
   }
   for(const item of rows){const option=el('option',(item.deleted?'回收站 · ':'')+(item.import_pending?'导入未完成 · ':'')+item.name+'（待采集 '+item.remaining+' / '+item.count+'）');option.value=item.id;select.append(option);}select.value=selected;
   trashButton.textContent=trash?'返回图库':'回收站';hiddenButton.textContent=hiddenView?'返回任务池':'隐藏列表';
   if(!selected){loaded=true;list=null;items.replaceChildren();count.textContent=hiddenView?'隐藏区暂无列表。':'暂无列表。';controls();return;}
   await readItems(true,id);
  }
  async function readItems(reset,id){
   const requested=selected;
   const data=await json('/api/reference-list?'+query({id:requested,offset:reset?0:next,limit:20,include_deleted:trash?1:0,deleted_only:trash&&requested==='all'?1:0}));if(!valid(id))return;
   if(data.list?.id!==requested)throw Error('返回的列表不一致，请刷新。');
   if(!reset&&data.revision!==revision)throw Error('图库已更新，请先刷新。');
   revision=data.revision;list=data.list;next=data.next_offset;loaded=true;
   if(reset){release();items.replaceChildren();preview.hidden=true;preview.querySelector('img').removeAttribute('src');}
   count.textContent=trash?(list.deleted?'回收站列表：图片与已采集数据保留。':'回收站：删除的图片可恢复，已采集数据保留。'):list.name+' · '+list.remaining+' / '+list.count+' 张待采集'+(list.readonly?' · 上传时自动新建列表':'');
   if(reset&&trash&&list.deleted){const restore=button('恢复此列表',()=>mutate({action:'restore_list',id:list.id},true));restore.id='phone-reference-restore-list';items.append(restore);}
   const jobs=[];
   for(const item of data.items){const card=el('article','','phone-reference-item'),thumb=el('img');thumb.alt=item.name;thumb.loading='lazy';const title=el('b',item.name),detail=el('small',item.deleted?'已删除':item.completed?'已完成采集':'待采集'),actions=row();
    actions.append(button('放大查看',()=>{if(!thumb.src)return;preview.querySelector('img').src=thumb.src;preview.hidden=false;preview.querySelector('button').focus();}));
    actions.append(button(item.deleted?'恢复图片':'删除图片',()=>{
     if(item.deleted){mutate({action:'restore_image',id:item.id});return;}
     actions.replaceChildren(el('span','删除这张图？'),button('确认删除',()=>mutate({action:'delete_image',id:item.id}),'danger'),button('取消',()=>loadItems(true)));
    }));card.append(thumb,title,detail,actions);items.append(card);jobs.push({thumb,item});
   }
   if(reset&&!data.items.length)items.append(el('p',trash?'回收站没有图片。':'此列表暂无参考图，点击“上传图片”添加。','phone-hint'));
   controls();
   showPendingSelection();
   // Keep optional decode traffic bounded and outside the controller loop.
   (async()=>{for(const {thumb,item} of jobs){if(!valid(id)||!thumb.isConnected)return;try{const blob=await (await request('/api/reference-thumbnail?'+query({id:item.id,include_deleted:item.deleted?1:0}),{headers:headers()})).blob();if(!valid(id)||!thumb.isConnected)continue;const url=URL.createObjectURL(blob);urls.add(url);thumb.src=url;}catch(error){if(error.name==='AbortError')return;}}})();
  }
  function invalidate(note='正在读取参考图…'){loaded=false;list=null;next=null;release();if(items)items.replaceChildren();if(preview){preview.hidden=true;preview.querySelector('img')?.removeAttribute('src');}if(count)count.textContent=note;}
  async function loadItems(reset){if(busy||!current())return;const id=generation;if(reset)invalidate();lock(true);try{await readItems(reset,id);}catch(error){if(valid(id))invalidate('列表未能加载，请刷新后再操作。');failure(error,id);}finally{if(valid(id))lock(false);}}
  async function mutate(body,newList=false){
   if(busy||!loaded||!current())return;const id=generation;lock(true);message('正在保存…');
   try{const data=await json('/api/reference-library',{...body,revision});if(!valid(id))return;revision=data.revision;await catalog(newList?data.id:selected,id);if(valid(id))message(newList?(singleList()?'列表已创建，可上传图片后点击“用此列表采集”。':'列表已创建，可上传图片后点击“启用混合抽取”。'):'已保存。');}
   catch(error){if(valid(id))invalidate('保存结果需要刷新确认，请先刷新图库。');failure(error,id);}finally{if(valid(id))lock(false);}
  }
  function showPendingSelection(){if(!selectionBox)return;selectionBox.replaceChildren();selectionBox.hidden=!pendingSelection&&!pendingBundles;
   if(pendingBundles){
    const plans=pendingBundles.plans;
    selectionBox.append(el('b',plans?'将导入 '+plans.length+' 个任务包，共 '+plans.reduce((n,p)=>n+p.images.length,0)+' 张任务图':'已选 '+pendingBundles.files.length+' 个 ZIP，等待解析'));
    if(plans)for(const plan of plans)selectionBox.append(el('p',plan.name+'：'+plan.images.length+' 张任务图，'+plan.scenes.length+' 个辅助文件'));
    const actions=row();actions.append(button(plans?'确认导入 ZIP':'解析 ZIP',()=>plans?uploadBundles():planBundles()),button('取消 ZIP 选择',()=>{pendingBundles=null;showPendingSelection();}));selectionBox.append(actions);return;
   }
   if(!pendingSelection)return;
   selectionBox.append(el('b','已选 '+pendingSelection.files.length+' 张图片，等待确认上传'),el('p','从相册返回后请核对上方目标列表，再继续上传。'));
   const actions=row();actions.append(button('上传这些图片',()=>{if(!pendingSelection||!loaded||busy)return;const chosen=pendingSelection.files;pendingSelection=null;showPendingSelection();uploadFiles(chosen);}),button('取消选择',()=>{pendingSelection=null;showPendingSelection();}));selectionBox.append(actions);
  }
  async function planBundles(){
   if(!pendingBundles||busy||!loaded||!current())return;const id=generation,chosen=pendingBundles.files;lock(true);message('正在读取 ZIP 目录…');
   try{
    if(chosen.length>50||chosen.reduce((n,f)=>n+f.size,0)>global.ReferenceBundles.LIMITS.batch)throw Error('每批最多50个 ZIP，总大小不超过4 GiB。');
    const plans=[];for(const file of chosen){plans.push(await global.ReferenceBundles.plan(file));if(!valid(id))return;}
    if(plans.reduce((n,p)=>n+p.images.length,0)>5000)throw Error('每批任务图最多5000张。');
    pendingBundles={files:chosen,plans};showPendingSelection();message('请核对任务包内容，再点击“确认导入 ZIP”。');
   }catch(error){if(valid(id)){pendingBundles=null;showPendingSelection();failure(error,id);}}
   finally{if(valid(id))lock(false);}
  }
  async function uploadBundles(){
   if(!pendingBundles?.plans||busy||!loaded||!current())return;const plans=pendingBundles.plans,id=generation;pendingBundles=null;showPendingSelection();lock(true);
   const total=plans.reduce((n,p)=>n+p.images.length,0);let saved=0,target=selected,done=0;
   transfer.hidden=false;progress.max=total;progress.value=0;lastTransfer={state:'running',kind:'bundles',listId:target,total,confirmed:0};
   try{
    for(const plan of plans){
     const manifest=[...plan.images,...plan.scenes].map(e=>({name:e.filename,size:e.size,crc:e.crc,kind:e.kind}));
     const created=await json('/api/reference-library',{action:'begin_bundle',name:plan.name,manifest,revision});if(!valid(id))return;revision=created.revision;target=created.id;selected=target;lastTransfer.listId=target;
     for(const entry of [...plan.images,...plan.scenes]){
      if(!valid(id))return;transferText.textContent=plan.name+'：'+(entry.kind==='image'?'正在上传任务图 '+(saved+1)+' / '+total:'保存辅助资料')+' · '+entry.filename;message(transferText.textContent);
      const raw=await global.ReferenceBundles.read(plan,entry);if(!valid(id))return;
      const path=entry.kind==='image'?'/api/reference-upload':'/api/reference-scene';
      const args={list_id:target,revision,name:entry.filename};if(entry.kind==='scene')args.task=plan.name;
      const result=await (await request(path+'?'+query(args),{method:'POST',headers:headers({'Content-Type':'application/octet-stream'}),body:raw,timeoutMs:40000})).json();
      if(!valid(id))return;revision=result.revision;if(entry.kind==='image'){saved++;progress.value=saved;lastTransfer.confirmed=saved;}
     }
     const completed=await json('/api/reference-library',{action:'complete_bundle',id:target,revision});if(!valid(id))return;revision=completed.revision;done++;
    }
    await catalog(target,id);if(valid(id)){lastTransfer.state='complete';transferText.textContent='已完成 '+done+' 个任务包、'+saved+' 张任务图。';message(transferText.textContent+(singleList()?' 请选择一个列表，点击“用此列表采集”。':' 已自动加入混合任务池。'));}
   }catch(error){if(valid(id)){lastTransfer.state='paused';invalidate('导入未完成，请刷新核对。');transferText.textContent='已确认 '+saved+' / '+total+' 张。';message(errorMessage(error)+' 已落盘内容保留；重新选择同一 ZIP 可继续未完成列表。');}}
   finally{if(valid(id))lock(false);}
  }
  async function uploadFiles(chosen){
   if(!chosen.length||busy||!loaded||!current())return;
   if(chosen.length>20){message('每次最多上传20张图片，可分批上传。');return;}
   if(chosen.some(file=>file.size>12*1024*1024)){message('每张图片最多12MiB，请减小后重试。');return;}
   const id=generation;let target=selected,saved=0,added=0,reused=0;lock(true);transfer.hidden=false;progress.max=chosen.length;progress.value=0;
   lastTransfer={state:'running',listId:target,total:chosen.length,confirmed:0};
   try{
    if(!list||list.readonly||list.deleted){const result=await json('/api/reference-library',{action:'create_list',name:newName.value.trim()||'手机上传参考图',revision});if(!valid(id))return;revision=result.revision;target=result.id;}
    selected=target;lastTransfer.listId=target;
    for(const file of chosen){if(!valid(id))return;transferText.textContent='正在上传 '+(saved+1)+' / '+chosen.length+'：'+file.name;message(transferText.textContent);
     const result=await (await request('/api/reference-upload?'+query({list_id:target,revision,name:file.name}),{method:'POST',headers:headers({'Content-Type':'application/octet-stream'}),body:file,timeoutMs:40000})).json();
     if(!valid(id))return;revision=result.revision;saved++;lastTransfer.confirmed=saved;progress.value=saved;if(result.deduplicated)reused++;else added++;
    }
    await catalog(target,id);if(valid(id)){lastTransfer.state='complete';transferText.textContent='已处理 '+saved+' 张：新增 '+added+' 张'+(reused?'，已存在 '+reused+' 张':'')+'。';message('已处理 '+saved+' 张图片。'+(singleList()?'点击“用此列表采集”，下一段仅从这个列表随机抽图。':'点击“启用混合抽取”，下一段将从多个启用列表随机抽图。'));}
   }catch(error){if(valid(id)){lastTransfer.state='paused';transferText.textContent='上传已暂停，已确认 '+saved+' / '+chosen.length+' 张。';invalidate('上传结果需要刷新确认，请先刷新图库。');message('已确认上传 '+saved+' 张。'+errorMessage(error)+' 请刷新核对后再上传剩余图片。');}}
   finally{if(valid(id))lock(false);}
  }
   async function scanUsb(){
    if(busy||usbActive()||!current())return;const id=generation;lock(true);
    try{
     usbInfo.textContent='正在扫描头环上的U盘…';
     const data=await (await request('/api/phone/usb-task-import/scan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({scan:true}),timeoutMs:30000})).json();if(!valid(id))return;
     usbData=data;usbSelected=new Set();usbRemove=new Set();usbChoices.replaceChildren();usbOldChoices.replaceChildren();usbProgress.hidden=true;
     for(const file of data.files){const label=el('label','','phone-pool-row'),check=el('input');check.type='checkbox';check.checked=false;check.dataset.zipId=file.id;
      check.onchange=()=>{if(check.checked)usbSelected.add(file.id);else usbSelected.delete(file.id);usbInfo.textContent='找到 '+data.files.length+' 个 ZIP，已选 '+usbSelected.size+' 个；旧列表默认保留。';controls();};
      label.append(check,document.createTextNode(file.volume+' / '+file.relative_path+'（'+(file.size_bytes/1024/1024).toFixed(1)+' MiB）'));usbChoices.append(label);}
     for(const old of data.old_lists||[]){const label=el('label','','phone-pool-row'),check=el('input');check.type='checkbox';check.checked=false;check.dataset.listId=old.id;check.onchange=()=>{if(check.checked)usbRemove.add(old.id);else usbRemove.delete(old.id);controls();};label.append(check,document.createTextNode(old.name+(old.readonly?'（系统列表，需先复制参考图）':'')));usbOldChoices.append(label);}
     usbInfo.textContent=data.files.length?'找到 '+data.files.length+' 个 ZIP；默认不删除旧列表。':'没有找到 ZIP，可只选择要移入回收站的旧列表。';
    }catch(error){if(valid(id)){usbData=null;usbInfo.textContent=errorMessage(error);}}
    finally{if(valid(id))lock(false);}
   }
   function applyUsb(){
    if(usbApply.disabled||!usbData||(!usbSelected.size&&!usbRemove.size))return;
    if(!global.confirm('导入 '+usbSelected.size+' 个任务包，并将 '+usbRemove.size+' 个旧列表移入回收站？其他旧列表会保留。'))return;
    pendingBundles=pendingSelection=null;showPendingSelection();
    usbSubmitting=true;controls();usbInfo.textContent='已提交，正在核实头环处理状态；请勿重复提交。';
    const id=generation,submitted=usbData,payload={scan_id:submitted.scan_id,zip_ids:[...usbSelected],remove_list_ids:[...usbRemove],catalog_revision:submitted.catalog_revision};usbSubmissionStamp=String(usbState.updated_at||'');usbData=null;usbSelected.clear();usbRemove.clear();
    Promise.resolve(action('usb_task_import',payload)).then(()=>{if(id!==generation)return;usbSubmitting=false;usbUnknown=true;usbInfo.textContent='正在核实头环处理状态；若状态仍未明确，请刷新页面后再操作。';controls();}).catch(()=>{if(id!==generation)return;usbSubmitting=false;usbUnknown=true;usbInfo.textContent='请求结果未知，请刷新页面核对头环状态后再操作；请勿重复提交。';controls();});
   }
   async function pollUsb(force=false){
    if(usbPolling||!current()||root.hidden||(!force&&Date.now()-usbPollAt<2000))return;
    const id=generation;usbPolling=true;usbPollAt=Date.now();
    try{
     const data=await (await request('/api/phone/usb-task-import/status')).json();if(!valid(id))return;
     usbState=data;usbStatusKnown=true;
     const stamp=String(data.updated_at||'');
     if((usbSubmitting||usbUnknown)&&stamp&&stamp!==usbSubmissionStamp&&['building','ready','committing','needs_recovery','complete','failed'].includes(data.state)){usbSubmitting=false;usbUnknown=false;}
     if(data.state!=='idle'){
      usbInfo.textContent=(data.message||data.state)+(data.total?' · '+data.done+' / '+data.total:'');
      usbProgress.hidden=!data.total;usbProgress.max=Math.max(1,data.total||1);usbProgress.value=data.done||0;
     }
     if(data.state==='complete'&&stamp!==usbCompleteSeen&&!busy){usbCompleteSeen=stamp;usbData=null;usbSelected.clear();usbRemove.clear();usbChoices.replaceChildren();usbOldChoices.replaceChildren();await load();}
     if(data.state==='idle'&&usbUnknown){usbInfo.textContent='服务器状态仍为待确认，请刷新页面查询后再操作。';}
     controls();
    }catch(error){if(valid(id)&&usbActive())usbInfo.textContent='连接中断，导入可能仍在头环继续；恢复连接后刷新查看，不要重复提交。';}
    finally{usbPolling=false;}
   }
   function update(snapshot){if(activeList&&!root.hidden){activeList.textContent='当前抽取范围：'+(snapshot.task_list?.name||'等待 Pi 确认');
    if(singleList())for(const node of sampling.querySelectorAll('input[type=radio]'))node.checked=node.dataset.listId===snapshot.task_list?.id;
    if(!busy&&loaded&&Number.isSafeInteger(snapshot.task_list?.catalog_revision)&&snapshot.task_list.catalog_revision>revision){invalidate('图库已在其他页面更新，请刷新后继续。');message('参考图库已更新，请点击刷新。');}
    controls();pollUsb();}}
  async function open(){suspend();const id=generation;selected=pendingSelection?.listId||(['interrupted','paused'].includes(lastTransfer?.state)?lastTransfer.listId:null)||activeTask()||status().task_list?.id||'all';resumeOffered=!!(pendingSelection||pendingBundles);trash=false;busy=false;ui();lock(true);message('正在连接参考图库…');
   usbStatusKnown=false;usbPollAt=0;usbSubmitting=false;usbUnknown=false;usbData=null;usbSelected.clear();usbRemove.clear();
   try{const access=await (await request('/api/phone/reference-access')).json();if(!valid(id))return;key=access.access_code;await catalog(selected,id);await pollUsb(true);if(valid(id))message(manualTasks()?'选择想先拍的列表；当前列表做完后自动进入下一个，已完成进度保留。':singleList()?'选择一个列表采集；每段仅在该列表内随机抽图，完成后手动换列表。':'勾选参与抽取的列表；每段在混合任务池随机抽图，重采保持同一张。');}
   catch(error){failure(error,id);}finally{if(valid(id))lock(false);}
  }
  return {suspend,open,update,resumeNeeded:()=>!!(pendingSelection||pendingBundles)&&!resumeOffered};
 };
})(window);
