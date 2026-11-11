'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(process.argv[2]||path.join(__dirname,'../src/orangepi_collect/control/phone_recording.js'),'utf8');
const nodes={};
function node(id){return nodes[id]||(nodes[id]={id,hidden:false,textContent:'',innerHTML:'',style:{},dataset:{},attributes:{},disabled:false,classList:{toggle(){}},
 setAttribute(k,v){this.attributes[k]=v},removeAttribute(k){delete this.attributes[k]},toggleAttribute(k,v){if(k==='hidden')this.hidden=v;else if(v)this.attributes[k]='';else delete this.attributes[k]}})}
function element(tag){return {tagName:tag.toUpperCase(),hidden:false,textContent:'',innerHTML:'',style:{},attributes:{},
 setAttribute(k,v){this.attributes[k]=v},removeAttribute(k){delete this.attributes[k]},toggleAttribute(k,v){if(k==='hidden')this.hidden=v;else if(v)this.attributes[k]='';else delete this.attributes[k]}}}
const header=node('header');header.insertAdjacentElement=(position,child)=>{header.inserted={position,child};};
const document={hidden:false,getElementById:node,createElement:element,querySelector(selector){return selector==='header'?header:null},querySelectorAll(){return []},addEventListener(){}};
const global={};vm.runInNewContext(source,{window:global,document,performance:{now:()=>1000},requestAnimationFrame:()=>1,cancelAnimationFrame(){},Date});
const overlay=global.createRecordingOverlay({});
const ready={state:'ready',revision:7,actions:['start'],worker:{recording_active:false},privacy:{phase:'idle',stop_confirmed:true,required:false}};
overlay.state({snapshot:ready,canStop:false,stale:false});
assert.equal(node('recording-hold-hint').hidden,true);assert.equal(node('privacy-cover').attributes.role,'status');
assert(!node('recording-countdown').textContent.includes('采集中'));
const idle={...ready,state:'idle',revision:8};
overlay.state({snapshot:idle,canStop:false,stale:false});
assert.equal(node('recording-hold-hint').hidden,true);assert.equal(node('privacy-cover').attributes.role,'status');
overlay.state({snapshot:idle,canStop:false,stale:true});
assert.equal(node('recording-hold-hint').hidden,true);
assert(node('recording-countdown').textContent.includes('连接刷新中'));
assert(node('recording-countdown').textContent.includes('状态待确认'));
assert(!node('recording-countdown').textContent.includes('采集中'));
overlay.state({snapshot:{state:'recording',revision:8,actions:['stop'],worker:{recording_active:true},privacy:{phase:'recording',stop_confirmed:false,required:true}},canStop:true,stale:false});
assert.equal(node('recording-hold-hint').hidden,false);assert.equal(node('privacy-cover').attributes.role,'button');
assert.equal(node('recording-countdown').textContent,'采集中');
const finalizing={state:'finalizing',revision:9,actions:[],worker:{recording_active:false,state:'checking',qc_scope:'full',progress:{completed:2,total:5,active:['cam_ros_left']}},privacy:{phase:'idle',stop_confirmed:true,required:false}};
overlay.state({snapshot:finalizing,canStop:false,stale:false});
assert.equal(header.inserted.position,'afterend');
assert.equal(header.inserted.child.id,'qc-progress-card');
assert.equal(header.inserted.child.hidden,false);
assert.equal(node('qc-stage-title').textContent,'质量检查中');
assert(node('qc-stage-detail').textContent.includes('已完成 2 / 5 项'));
assert.equal(node('qc-stage-progress').max,5);assert.equal(node('qc-stage-progress').value,2);
overlay.state({snapshot:ready,canStop:false,stale:false});
assert.equal(header.inserted.child.hidden,true);
assert.equal(node('recording-hold-hint').hidden,true);assert.equal(node('privacy-cover').attributes.role,'status');
assert.equal(node('recording-stop-ring').hidden,true);
console.log('PASS ready, idle, stale connection, recording, finalizing QC and stopped overlay states');
