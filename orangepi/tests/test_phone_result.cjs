/* Real regression: a restart can expose an older review without a current take. */
'use strict';
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const global={};vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../src/orangepi_collect/control/phone_shell.js'),'utf8'),{window:global});
const describe=global.describePhoneRecordingResult,review={session:'old-take',qc_level:'WARN',warn_count:7,error_count:0,videos:[{name:'camera'}]};
let result=describe({review},'确认设备');assert.equal(result.title,'最近可回看采集结果');assert(result.text.includes('历史采集结果，批次：old-take'));assert(result.text.includes('尚未准备新段'));
result=describe({session:'/data/new-take',review},'准备中');assert(result.text.includes('当前任务标识：new-take'));assert(result.text.includes('历史采集结果，批次：old-take'));
result=describe({session:'/data/old-take',review,last_completed:{session:'old-take',outcome:'success'}},'完成');assert.equal(result.title,'本段已确认保存');assert(!result.text.includes('历史采集结果'));
result=describe({review,last_completed:{session:'other-take',outcome:'success'}},'确认设备');assert(!result.title.includes('已保存'));assert(!result.text.includes('已确认此批次保存'));
result=describe({},'准备中');assert(!result.text.includes('质量检查：WARN'));assert.equal(result.qc_level,null);
console.log('PASS historical/current review attribution and batch-bound save confirmation');
