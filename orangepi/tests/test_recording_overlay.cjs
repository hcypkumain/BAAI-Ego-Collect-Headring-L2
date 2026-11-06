'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(process.argv[2]||path.join(__dirname,'../src/orangepi_collect/control/phone_recording.js'),'utf8');
const nodes={},created=[];
function node(id){return nodes[id]||(nodes[id]={hidden:false,textContent:'',style:{},dataset:{},attributes:{},classList:{toggle(){}},
 setAttribute(k,v){this.attributes[k]=v},removeAttribute(k){delete this.attributes[k]},toggleAttribute(k,v){if(k==='hidden')this.hidden=v}})}
const document={hidden:false,getElementById:node,addEventListener(){},
 createElement(){const value=node('created-'+created.length);created.push(value);return value},
 querySelector(){return {insertAdjacentElement(){}}},querySelectorAll(){return []}};
const global={};vm.runInNewContext(source,{window:global,document,performance:{now:()=>1000},requestAnimationFrame:()=>1,cancelAnimationFrame(){},Date});
const overlay=global.createRecordingOverlay({});
const idle={state:'ready',privacy:{phase:'idle',stop_confirmed:true,required:false}};
overlay.state({snapshot:idle,canStop:false,stale:false});
assert.equal(node('recording-hold-hint').hidden,true);assert.equal(node('privacy-cover').attributes.role,'status');
assert(!node('recording-countdown').textContent.includes('采集中'));
overlay.state({snapshot:idle,canStop:false,stale:true});
assert.equal(node('recording-hold-hint').hidden,true);assert(node('recording-countdown').textContent.includes('连接刷新中'));
overlay.state({snapshot:{state:'recording',privacy:{phase:'recording',required:true}},canStop:true,stale:false});
assert.equal(node('recording-hold-hint').hidden,false);assert.equal(node('privacy-cover').attributes.role,'button');
assert.equal(node('recording-countdown').textContent,'采集中');
overlay.state({snapshot:idle,canStop:false,stale:false});
assert.equal(node('recording-hold-hint').hidden,true);assert.equal(node('privacy-cover').attributes.role,'status');
assert.equal(node('recording-stop-ring').hidden,true);
overlay.state({snapshot:{state:'finalizing',privacy:idle.privacy,worker:{state:'checking',qc_scope:'emg_only',progress:{completed:1,total:2,active:['emg_right']}}},canStop:false,stale:false});
assert.equal(created[0].hidden,false);assert.equal(node('qc-stage-title').textContent,'肌电质检中');
assert(node('qc-stage-detail').textContent.includes('其他模态待 Linux 主机质检'));
assert.equal(node('qc-stage-progress').value,1);assert.equal(node('qc-stage-progress').max,2);
overlay.renderFault(new Error('synthetic rendering failure'));assert.equal(node('privacy-cover').dataset.renderFault,'true');
assert(node('recording-countdown').textContent.includes('页面显示异常'));
overlay.renderFault(null);assert.equal(node('privacy-cover').dataset.renderFault,'false');
console.log('PASS idle, stale connection, recording, stopped, partial QC progress and render fault states');
