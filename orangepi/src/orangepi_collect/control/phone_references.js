/* Reference management uses the existing durable catalog and collection owner. */
(function(global){
 'use strict';
 global.createPhoneReferenceLibrary=function({root,request,status,action,message,current}){
  let generation=0,key='',revision=0,selected='all',list=null,trash=false,next=null,busy=false,loaded=false;
  let pickerTarget=null,pendingSelection=null,resumeOffered=false;
  let lastTransfer=null;
  let pendingBundles=null;
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
  let select,uploadLabel,bundleLabel,use,refresh,trashButton,newName,createButton,items,count,more,preview,activeList,selectionBox,transfer,transferText,progress;
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
  function controls(){if(!use)return;const editable=!busy&&loaded&&!!key;
   use.disabled=!editable||trash||!list||list.deleted||list.import_pending||list.remaining===0||!(status().actions||[]).includes('reference_list');
   files.disabled=!editable||trash;uploadLabel.classList.toggle('disabled',files.disabled);createButton.disabled=!editable||trash;newName.disabled=!editable||trash;more.hidden=next===null;
   bundles.disabled=!editable||trash;bundleLabel.classList.toggle('disabled',bundles.disabled);
   for(const node of root.querySelectorAll('.phone-reference-item button,.phone-reference-selection button'))node.disabled=!editable;
  }
  async function json(path,body){return (await request(path,{headers:headers(body?{'Content-Type':'application/json'}:{}),...(body?{method:'POST',body:JSON.stringify(body)}:{})})).json();}
  function errorMessage(error){return error.name==='AbortError'?'请求超时，请刷新确认操作结果。':error.name==='TypeError'?'网络连接中断，请恢复后刷新核对。':error.message;}
  function failure(error,id){if(valid(id))message(errorMessage(error));}
  function ui(){
   root.replaceChildren();root.hidden=false;
   const label=el('label','参考图列表');select=el('select');select.id='phone-reference-select';select.setAttribute('aria-label','参考图列表');label.append(select);
   refresh=button('刷新',()=>key?load(selected):open());trashButton=button('回收站',()=>{trash=!trash;load('all');});const toolbar=row();toolbar.append(refresh,trashButton);
   use=button('用此列表采集',()=>{if(!use.disabled)action('reference_list',{list_id:selected,catalog_revision:revision});},'primary');use.id='phone-reference-use';
   count=el('p','','phone-hint');count.id='phone-reference-count';
   activeList=el('p','','phone-hint');activeList.id='phone-reference-active-list';
   const upload=row();uploadLabel=el('label','上传图片','phone-reference-upload');uploadLabel.htmlFor='phone-reference-files';
   bundleLabel=el('label','从 ZIP 导入','phone-reference-upload');bundleLabel.htmlFor='phone-reference-bundles';bundleLabel.id='phone-reference-bundle-entry';upload.append(uploadLabel,bundleLabel,use);
   const create=row();newName=el('input');newName.placeholder='新列表名称';newName.maxLength=128;newName.setAttribute('aria-label','新列表名称');newName.id='phone-reference-name';
   createButton=button('新建列表',async()=>{if(busy||!loaded)return;const name=newName.value.trim();if(!name){message('请输入新列表名称。');newName.focus();return;}await mutate({action:'create_list',name},true);});create.append(newName,createButton);
   selectionBox=el('section','','phone-reference-selection');selectionBox.id='phone-reference-selection';selectionBox.hidden=true;
   transfer=el('section','','phone-reference-transfer');transfer.id='phone-reference-transfer';transfer.hidden=true;transfer.setAttribute('role','status');transfer.setAttribute('aria-live','polite');
   transferText=el('p');progress=el('progress');progress.id='phone-reference-progress';transfer.append(transferText,progress);
   if(lastTransfer&&['interrupted','paused'].includes(lastTransfer.state)){transfer.hidden=false;progress.max=lastTransfer.total;progress.value=lastTransfer.confirmed;transferText.textContent='上次上传已暂停，已确认 '+lastTransfer.confirmed+' / '+lastTransfer.total+' 张。未确认的图片请先刷新核对。';}
   items=el('div','','phone-reference-items');items.id='phone-reference-items';more=button('加载更多',()=>loadItems(false));
   preview=el('div','','phone-reference-preview');preview.hidden=true;preview.setAttribute('role','dialog');preview.setAttribute('aria-modal','true');preview.setAttribute('aria-label','参考图预览');
   const close=button('关闭预览',()=>{preview.hidden=true;preview.querySelector('img')?.removeAttribute('src');});const image=el('img');image.alt='参考图预览';preview.append(close,image);
   root.append(label,toolbar,count,activeList,upload,el('p','ZIP 按文件名建立任务列表；摆放总览、YAML 和 CSV 作为辅助资料保存，不进入随机任务池。','phone-hint'),selectionBox,transfer,create,items,more,preview);showPendingSelection();update(status());
   select.onchange=()=>{selected=select.value;loadItems(true);};
  }
  async function load(wanted=selected){
   if(busy||!current())return;const id=generation;invalidate();lock(true);message('正在读取参考图库…');
   try{await catalog(wanted,id);if(valid(id))message('每段从选中列表随机抽取未完成的图；重采仍用同一张。');}
   catch(error){if(valid(id))invalidate('列表未能加载，请刷新后再操作。');failure(error,id);}finally{if(valid(id))lock(false);}
  }
  async function catalog(wanted,id){
   let rows=[],offset=0,version=null;
   do{const data=await json('/api/reference-library?'+query({offset,limit:50,include_deleted:trash?1:0}));if(!valid(id))return;
    if(version!==null&&version!==data.revision)throw Error('图库已更新，请点击刷新。');version=data.revision;rows.push(...data.lists);offset=data.next_offset;
   }while(offset!==null);
   revision=version;selected=rows.some(item=>item.id===wanted)?wanted:'all';select.replaceChildren();
   for(const item of rows){const option=el('option',(item.deleted?'回收站 · ':'')+(item.import_pending?'导入未完成 · ':'')+item.name+'（待采集 '+item.remaining+' / '+item.count+'）');option.value=item.id;select.append(option);}select.value=selected;
   trashButton.textContent=trash?'返回图库':'回收站';await readItems(true,id);
  }
  async function readItems(reset,id){
   const requested=selected;
   const data=await json('/api/reference-list?'+query({id:requested,offset:reset?0:next,limit:20,include_deleted:trash?1:0,deleted_only:trash&&requested==='all'?1:0}));if(!valid(id))return;
   if(data.list?.id!==requested)throw Error('返回的列表不一致，请刷新。');
   if(!reset&&data.revision!==revision)throw Error('图库已更新，请先刷新。');
   revision=data.revision;list=data.list;next=data.next_offset;loaded=true;
   if(reset){release();items.replaceChildren();preview.hidden=true;preview.querySelector('img').removeAttribute('src');}
   count.textContent=trash?'回收站：删除的图片可恢复，已采集数据保留。':list.name+' · '+list.remaining+' / '+list.count+' 张待采集'+(list.readonly?' · 上传时自动新建列表':'');
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
   try{const data=await json('/api/reference-library',{...body,revision});if(!valid(id))return;revision=data.revision;await catalog(newList?data.id:selected,id);if(valid(id))message(newList?'列表已创建，可上传图片后点击“用此列表采集”。':'已保存。');}
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
    await catalog(target,id);if(valid(id)){lastTransfer.state='complete';transferText.textContent='已完成 '+done+' 个任务包、'+saved+' 张任务图。';message(transferText.textContent+' 可选择列表用于采集。');}
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
    await catalog(target,id);if(valid(id)){lastTransfer.state='complete';transferText.textContent='已处理 '+saved+' 张：新增 '+added+' 张'+(reused?'，已存在 '+reused+' 张':'')+'。';message('已处理 '+saved+' 张图片。点击“用此列表采集”，下一段将从此列表随机抽图。');}
   }catch(error){if(valid(id)){lastTransfer.state='paused';transferText.textContent='上传已暂停，已确认 '+saved+' / '+chosen.length+' 张。';invalidate('上传结果需要刷新确认，请先刷新图库。');message('已确认上传 '+saved+' 张。'+errorMessage(error)+' 请刷新核对后再上传剩余图片。');}}
   finally{if(valid(id))lock(false);}
  }
  function update(snapshot){if(activeList&&!root.hidden){activeList.textContent='当前采集列表：'+(snapshot.task_list?.name||'等待 Pi 确认');
    if(!busy&&loaded&&Number.isSafeInteger(snapshot.task_list?.catalog_revision)&&snapshot.task_list.catalog_revision>revision){invalidate('图库已在其他页面更新，请刷新后继续。');message('参考图库已更新，请点击刷新。');}
    controls();}}
  async function open(){suspend();const id=generation;selected=pendingSelection?.listId||(['interrupted','paused'].includes(lastTransfer?.state)?lastTransfer.listId:null)||status().task_list?.id||'all';resumeOffered=!!(pendingSelection||pendingBundles);trash=false;busy=false;ui();lock(true);message('正在连接参考图库…');
   try{const access=await (await request('/api/phone/reference-access')).json();if(!valid(id))return;key=access.access_code;await catalog(selected,id);if(valid(id))message('上传并选择列表后，每段随机抽取一张未完成的参考图。');}
   catch(error){failure(error,id);}finally{if(valid(id))lock(false);}
  }
  return {suspend,open,update,resumeNeeded:()=>!!(pendingSelection||pendingBundles)&&!resumeOffered};
 };
})(window);
