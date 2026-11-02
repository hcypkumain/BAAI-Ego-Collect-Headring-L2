/* Read task ZIPs one member at a time; never extract paths to the filesystem. */
(function(root){
 'use strict';
 const LIMITS={zip:256*1024*1024,batch:4*1024*1024*1024,entries:10000,images:5000,image:12*1024*1024,scene:2*1024*1024};
 const IMAGE=/\.(png|jpe?g|webp)$/i,SCENE=/\.(ya?ml|csv)$/i,OVERVIEW=/_placements_overview\.png$/i;
 const table=new Uint32Array(256);
 for(let n=0;n<256;n++){let c=n;for(let k=0;k<8;k++)c=(c&1)?0xedb88320^(c>>>1):c>>>1;table[n]=c>>>0;}
 function crc32(bytes){let c=0xffffffff;for(const b of bytes)c=table[(c^b)&255]^(c>>>8);return (c^0xffffffff)>>>0;}
 function nameOf(raw,flags){
  try{return new TextDecoder('utf-8',{fatal:true}).decode(raw);}
  catch(error){if(flags&0x800)throw Error('ZIP 文件名不是有效 UTF-8。');return new TextDecoder('gb18030',{fatal:true}).decode(raw);}
 }
 function safeName(value){
  const name=value.replace(/\\/g,'/');
  if(!name||name.startsWith('/')||/^[A-Za-z]:/.test(name)||/[\x00-\x1f]/.test(name)||name.split('/').some(p=>p==='..'))throw Error('ZIP 包含不安全路径。');
  return name;
 }
 const base=name=>name.split('/').pop();
 async function plan(file){
  if(!file||file.size<22||file.size>LIMITS.zip)throw Error('每个 ZIP 最多 256 MiB，且必须是有效压缩包。');
  const tail=new Uint8Array(await file.slice(Math.max(0,file.size-65557)).arrayBuffer()),v=new DataView(tail.buffer);
  let end=-1;
  for(let i=tail.length-22;i>=0;i--)if(v.getUint32(i,true)===0x06054b50&&i+22+v.getUint16(i+20,true)===tail.length){end=i;break;}
  if(end<0)throw Error('找不到 ZIP 目录，请确认文件上传完整。');
  const count=v.getUint16(end+10,true),size=v.getUint32(end+12,true),offset=v.getUint32(end+16,true);
  if(v.getUint16(end+4,true)||v.getUint16(end+6,true)||v.getUint16(end+8,true)!==count||count===65535||count>LIMITS.entries||size>4*1024*1024||offset+size>file.size-tail.length+end)throw Error('不支持分卷、ZIP64 或过大的 ZIP 目录。');
  const raw=new Uint8Array(await file.slice(offset,offset+size).arrayBuffer()),view=new DataView(raw.buffer),entries=[];let pos=0;
  for(let i=0;i<count;i++){
   if(pos+46>raw.length||view.getUint32(pos,true)!==0x02014b50)throw Error('ZIP 目录损坏。');
   const flags=view.getUint16(pos+8,true),method=view.getUint16(pos+10,true),nl=view.getUint16(pos+28,true),el=view.getUint16(pos+30,true),cl=view.getUint16(pos+32,true);
   if(pos+46+nl+el+cl>raw.length)throw Error('ZIP 文件名损坏。');
   const name=safeName(nameOf(raw.subarray(pos+46,pos+46+nl),flags)),entry={name,flags,method,crc:view.getUint32(pos+16,true),compressed:view.getUint32(pos+20,true),size:view.getUint32(pos+24,true),local:view.getUint32(pos+42,true),attrs:view.getUint32(pos+38,true)};
   entries.push(entry);pos+=46+nl+el+cl;
  }
  const images=[],scenes=[];let ignored=0,overviews=0;const names=new Set();
  for(const entry of entries){
   if(entry.name.endsWith('/')||entry.name.startsWith('__MACOSX/')||base(entry.name).startsWith('.'))continue;
   const overview=OVERVIEW.test(entry.name),kind=overview||SCENE.test(entry.name)?'scene':IMAGE.test(entry.name)?'image':null;
   if(!kind){ignored++;continue;}
   if((entry.flags&1)||![0,8].includes(entry.method)||((entry.attrs>>>16)&0xf000)===0xa000)throw Error('ZIP 含加密文件、链接或不支持的压缩方式：'+base(entry.name));
   if(!entry.size||entry.size>(kind==='image'?LIMITS.image:LIMITS.scene)||entry.local+30+entry.compressed>offset)throw Error('ZIP 成员过大或数据范围无效：'+base(entry.name));
   const filename=base(entry.name);if(names.has(filename))throw Error('ZIP 中存在同名成员：'+filename);names.add(filename);
   entry.filename=filename;entry.kind=kind;(kind==='image'?images:scenes).push(entry);if(overview)overviews++;
  }
  if(!images.length||images.length>LIMITS.images||scenes.length>4)throw Error('任务包必须有图片，最多5000张图片和4个辅助文件。');
  images.sort((a,b)=>a.filename.localeCompare(b.filename,'zh-Hans-CN',{numeric:true}));
  return {file,name:file.name.replace(/\.zip$/i,''),images,scenes,ignored,overviews};
 }
 async function read(plan,entry){
  const file=plan.file,header=new DataView(await file.slice(entry.local,entry.local+30).arrayBuffer());
  if(header.byteLength!==30||header.getUint32(0,true)!==0x04034b50||header.getUint16(6,true)!==entry.flags||header.getUint16(8,true)!==entry.method)throw Error('ZIP 条目头损坏：'+entry.filename);
  const nl=header.getUint16(26,true),el=header.getUint16(28,true),start=entry.local+30+nl+el;
  if(start+entry.compressed>file.size)throw Error('ZIP 条目被截断：'+entry.filename);
  const localName=new Uint8Array(await file.slice(entry.local+30,entry.local+30+nl).arrayBuffer());
  if(safeName(nameOf(localName,entry.flags))!==entry.name)throw Error('ZIP 成员名称不一致。');
  let stream=file.slice(start,start+entry.compressed).stream();
  if(entry.method===8){try{stream=stream.pipeThrough(new DecompressionStream('deflate-raw'));}catch(error){throw Error('当前浏览器不支持此 ZIP 解压，请用新版 Chrome。');}}
  const reader=stream.getReader(),chunks=[];let length=0;
  try{for(;;){const item=await reader.read();if(item.done)break;length+=item.value.byteLength;if(length>entry.size)throw Error('ZIP 解压长度异常。');chunks.push(item.value);}}
  catch(error){await reader.cancel().catch(()=>{});throw error;}
  if(length!==entry.size)throw Error('ZIP 解压不完整：'+entry.filename);
  const bytes=new Uint8Array(length);let at=0;for(const chunk of chunks){bytes.set(chunk,at);at+=chunk.length;}
  if(crc32(bytes)!==entry.crc)throw Error('ZIP 校验失败：'+entry.filename);
  return bytes;
 }
 root.ReferenceBundles={plan,read,crc32,LIMITS};
 if(typeof module==='object')module.exports=root.ReferenceBundles;
})(typeof window==='undefined'?globalThis:window);
