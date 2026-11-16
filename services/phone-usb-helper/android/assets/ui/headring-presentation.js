/* Local appearance and explicit in-app navigation only; no device requests. */
(function () {
 'use strict';
 const config=window.__headringUi;
 if(!config||config.version!==6||!/^\/__headring\/ui\/connection\?nonce=[a-zA-Z0-9-]+$/.test(config.connection))return;
 const paths={
  overview:'M4 4h6v6H4z M14 4h6v6h-6z M4 14h6v6H4z M14 14h6v6h-6z',
  signals:'M3 12h4l2-7 5 14 2-7h5',
  preview:'M8 6l2-2h4l2 2h4v14H4V6z M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0',
  results:'M7 3h7l4 4v14H6V3h1 M14 3v5h5 M9 12h6 M9 16h6',
  connections:'M12 3v15 M9 6l3-3 3 3 M12 11L6 8V5 M12 15l6-3V8 M10 20a2 2 0 1 0 4 0 2 2 0 0 0-4 0',
  settings:'M4 7h9 M17 7h3 M4 17h3 M11 17h9 M13 4v6 M7 14v6',
  arrow:'M5 12h14 M14 7l5 5-5 5'
 };
 const icon=name=>{
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('width','24');svg.setAttribute('height','24');svg.setAttribute('fill','none');svg.setAttribute('stroke','currentColor');svg.setAttribute('stroke-width','1.8');svg.setAttribute('stroke-linecap','round');svg.setAttribute('stroke-linejoin','round');svg.setAttribute('aria-hidden','true');svg.setAttribute('focusable','false');
  const path=document.createElementNS(svg.namespaceURI,'path');path.setAttribute('d',paths[name]);svg.append(path);return svg;
 };
 function install(){
  const body=document.body,nav=document.getElementById('phone-nav'),shell=document.getElementById('phone-shell');
  if(!body?.classList.contains('phone-app')||!nav||!shell||!document.getElementById('privacy-cover'))return false;
  let style=document.getElementById('headring-ui-theme');
  if(!style){style=document.createElement('style');style.id='headring-ui-theme';document.head.append(style);}
  if(style.textContent!==config.css)style.textContent=config.css;
  body.dataset.hrTheme='6';
  if(body.dataset.hrMotionReady!=='6')requestAnimationFrame(()=>requestAnimationFrame(()=>{body.dataset.hrMotionReady='6';}));
  const profile=document.getElementById('phone-mode-emg')?.closest('.card');if(profile)profile.classList.add('hr-profile');
  const names={overview:'总览',signals:'波形',preview:'相机',results:'结果',connections:'连接'};
  for(const [key,label] of Object.entries(names)){
   const button=document.getElementById('phone-tab-'+key);if(!button||button.dataset.hrDecorated==='6')continue;
   const original=button.textContent;const text=document.createElement('span');text.textContent=label;
   button.replaceChildren(icon(key),text);button.setAttribute('aria-label',original);button.dataset.hrDecorated='6';
  }
  const title=document.querySelector('body.phone-app>header h1');if(title&&title.dataset.hrDecorated!=='6'){title.textContent='头环采集';title.dataset.hrDecorated='6';}
  const header=document.querySelector('body.phone-app>header');
  const settings=header?.querySelector('button:not(#phone-transport-status)');
  if(settings&&settings.dataset.hrDecorated!=='6'){
   const label=document.createElement('span');label.className='hr-sr-only';label.textContent=settings.textContent||'采集设置';settings.replaceChildren(icon('settings'),label);settings.setAttribute('aria-label','打开采集设置');settings.classList.add('hr-icon-button');settings.dataset.hrDecorated='6';
  }
  const connection=document.querySelector('#phone-page-connections .phone-connections-card');
  if(connection){
   let link=document.getElementById('headring-native-connection');
   if(!link){
    link=document.createElement('a');link.id='headring-native-connection';link.className='hr-native-entry';
    const text=document.createElement('span');const heading=document.createElement('b');heading.textContent='手机 USB 连接管理';const description=document.createElement('small');description.textContent='搜索、选择头环与手机连接偏好';text.append(heading,description);link.append(icon('connections'),text,icon('arrow'));
    document.getElementById('phone-page-connections').prepend(link);
   }
   link.href=config.connection;
  }
  return true;
 }
 if(install()){if(window.__headringUiObserver){window.__headringUiObserver.disconnect();window.__headringUiObserver=null;}return;}
 if(window.__headringUiObserver)return;
 // Some supported shells initialize after page commit. Observe only structural
 // setup and stop after five seconds, never the canvas or live signal content.
 const observer=new MutationObserver(()=>{if(install()){observer.disconnect();window.__headringUiObserver=null;}});
 window.__headringUiObserver=observer;observer.observe(document.documentElement,{childList:true,subtree:true});
 setTimeout(()=>{observer.disconnect();if(window.__headringUiObserver===observer)window.__headringUiObserver=null;},5000);
})();
