/* Local take retention only; no cloud requests or automatic upload. */
(function(global){
 'use strict';
 global.createPhoneRecordingManager=function({root,request,status,action,refresh,message,current}){
  let version=0,busy=false,day='today',offset=0,next=null;
  let cloud=null,localRoot=null;
  const el=(tag,text,classes)=>{const node=document.createElement(tag);if(text)node.textContent=text;if(classes)node.className=classes;return node;};
  const button=(label,fn)=>{const node=el('button',label);node.type='button';node.onclick=fn;return node;};
  const valid=id=>id===version&&current();
  let select,list,summary,more;
  function controls(){const scope=localRoot||root;for(const node of scope.querySelectorAll('button,select'))node.disabled=busy;for(const node of scope.querySelectorAll('[data-delete-take]'))node.disabled=busy||!(status().actions||[]).includes('delete_recording');}
  function lock(value){busy=value;controls();}
  function suspend(){cloud?.suspend();cloud=null;version++;root.hidden=true;root.replaceChildren();busy=false;localRoot=null;}
  async function load(reset=true){if(busy||!current())return;const id=version;lock(true);if(reset){offset=0;list.replaceChildren();summary.textContent='正在读取采集条目…';more.hidden=true;}
   try{const data=await(await request('/api/phone/recordings?'+new URLSearchParams({day,offset}))).json();if(!valid(id))return;
    if(reset)list.replaceChildren();summary.textContent=(day==='today'?'今日':'全部')+'：保留 '+data.retained_count+' 段 / 共 '+data.total+' 段。成功/WARN标记已采集；保留ERROR不计成功，删除会重新开放参考图。';
    for(const item of data.items){const row=el('article','','phone-take'),title=el('b',item.reference_name),info=el('p',new Date(item.created_unix*1000).toLocaleString()+' · '+Math.max(0,Number(item.duration)||0).toFixed(1)+' 秒 · '+item.id),state=el('small',item.retention_kind==='pending_qc'?'已保存 · 待主机质检 · 暂不重复抽取':item.retention_kind==='unsuccessful'?'未成功数据已保留 · '+(item.qc_level||item.status)+' · 本段不计为参考图成功':item.retained?'已保留，晚间上传用 · 参考图已采集（WARN不判失败）':'未成功/未确认保留 · 可保留错误数据或删除');
     const actions=el('div','','phone-row'),remove=button('作废并删除本段',()=>{
      actions.replaceChildren(el('p','删除本段的全部相机及同步数据？对应参考图将重新开放采集，本段移出上传清单。'),button('确认删除',()=>discard(item)),button('取消',()=>load(true)));
      for(const node of actions.querySelectorAll('button'))node.dataset.deleteTake='';controls();
     });remove.dataset.deleteTake='';if(!item.retained&&['error','qc_error','failed','interrupted'].includes(item.status)){const keep=button('保留未成功数据',()=>discard(item,true));keep.dataset.deleteTake='';actions.append(keep);}actions.append(remove);row.append(title,info,state,actions);list.append(row);
    }
    if(reset&&!data.items.length)list.append(el('p','暂无采集条目。保存新采集后会在这里出现。','phone-hint'));
    next=data.next_offset;more.hidden=next===null;
   }catch(error){if(valid(id)&&error.name!=='AbortError')message(error.message);}finally{if(valid(id))lock(false);}
  }
  async function discard(item,retain=false){if(busy||!current())return;const id=version,before=status().result?.time||0;lock(true);message(retain?'正在保留未成功数据，参考图仍可重采…':'正在删除本段并释放参考图…');
   try{
    if(!await action(retain?'retain_error_recording':'delete_recording',{session_id:item.id,token:item.token}))throw Error('本次操作未提交，请刷新当前状态后重试。');
    const deadline=performance.now()+20000;
    while(valid(id)){
     await refresh?.();const state=status();if(state.result?.time>before&&!state.busy){if(!state.result.ok)throw Error(state.result.error||'删除未完成');break;}
     if(performance.now()>deadline)throw Error('操作结果尚未确认，请先刷新，避免重复提交。');await new Promise(resolve=>setTimeout(resolve,150));
    }
    if(!valid(id))return;lock(false);await load(true);if(valid(id))message(retain?'未成功数据已保留；ERROR标记与参考图未成功状态保持，可切换下一张。':'已删除本段采集并重新核对参考图状态；数据已移入回收站，不再计入晚间上传。');
   }catch(error){if(valid(id))message(error.message);}finally{if(valid(id))lock(false);}
  }
  async function manifest(){if(busy||!current())return;const id=version;lock(true);try{const response=await request('/api/phone/upload-manifest?'+new URLSearchParams({day})),blob=await response.blob();if(!valid(id))return;const url=URL.createObjectURL(blob),link=el('a');link.href=url;link.download='retained-recordings-'+(day==='today'?new Date().toLocaleDateString('sv-SE'):day)+'.json';document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);message('已导出本地保留条目清单。请按清单上传，旧归档和回收站不在清单中。');}catch(error){if(valid(id))message(error.message);}finally{if(valid(id))lock(false);}}
  return {suspend,update(){if(!root.hidden){controls();cloud?.update();}},async open(){suspend();root.hidden=false;day='today';offset=0;next=null;
   const toolbar=el('div','','phone-row');select=el('select');select.setAttribute('aria-label','采集日期范围');for(const [value,label] of [['today','今日采集'],['all','全部新采集']]){const option=el('option',label);option.value=value;select.append(option);}select.onchange=()=>{day=select.value;load(true);};
   toolbar.append(select,button('刷新',()=>load(true)),button('导出保留清单',manifest));summary=el('p','','phone-hint');list=el('div','','phone-takes');more=button('加载更多',()=>{offset=next;load(false);});more.hidden=true;
   const cloudRoot=el('section');localRoot=el('section');localRoot.append(el('h3','本地采集管理 · 作废与重采'),el('p','作废会释放参考图。上传后的空间清理请使用上方“清理已上传本地数据”，保留任务完成记录。','phone-hint'),toolbar,summary,list,more);root.append(cloudRoot,localRoot);
   cloud=global.createPhoneCloudUploads?.({root:cloudRoot,request,status,message,current:()=>!root.hidden&&current()});cloud?.open();await load(true);
  }};
 };
})(window);
