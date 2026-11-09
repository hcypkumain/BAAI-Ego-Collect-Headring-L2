'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(process.argv[2]||path.join(__dirname,'../src/orangepi_collect/control/phone_recording.js'),'utf8');
const nodes={};
function node(id){return nodes[id]||(nodes[id]={hidden:false,textContent:'',style:{},attributes:{},classList:{toggle(){}},
 setAttribute(k,v){this.attributes[k]=v},toggleAttribute(k,v){if(k==='hidden')this.hidden=v}})}
const document={hidden:false,getElementById:node,addEventListener(){}};
const global={};vm.runInNewContext(source,{window:global,document,performance:{now:()=>1000},requestAnimationFrame:()=>1,cancelAnimationFrame(){},Date});
const overlay=global.createRecordingOverlay({});
const idle={state:'ready',privacy:{phase:'idle',stop_confirmed:true,required:false}};
overlay.state({snapshot:idle,canStop:false,stale:false});
assert.equal(node('recording-hold-hint').hidden,true);assert.equal(node('privacy-cover').attributes.role,'status');
assert(!node('recording-countdown').textContent.includes('采集中'));
overlay.state({snapshot:idle,canStop:false,stale:true});
assert.equal(node('recording-hold-hint').hidden,true);assert(node('recording-countdown').textContent.includes('恢复连接'));
overlay.state({snapshot:{state:'recording',privacy:{phase:'recording',required:true}},canStop:true,stale:false});
assert.equal(node('recording-hold-hint').hidden,false);assert.equal(node('privacy-cover').attributes.role,'button');
assert.equal(node('recording-countdown').textContent,'采集中');
overlay.state({snapshot:idle,canStop:false,stale:false});
assert.equal(node('recording-hold-hint').hidden,true);assert.equal(node('privacy-cover').attributes.role,'status');
assert.equal(node('recording-stop-ring').hidden,true);
console.log('PASS idle, stale connection, recording and stopped overlay states');
