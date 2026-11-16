"""Fail-closed, resumable multi-profile export pipeline.

The caller owns the controller exclusion and a live USB mount lease for the
whole run. Each selected item has an immutable mode, source and transaction ID.
"""
import copy
from pathlib import Path
from cloud_common import CloudError, atomic, read_json, check_source
from usb_export_copy import copy_recording, _verify
from usb_export_jobs import ExportJob
from usb_export_ledger import commit_credential
from usb_export_cleanup import cleanup
from usb_export_profile_store import ProfileStore


class ExportPipeline:
    def __init__(self, code_base, usb, store_factory=ProfileStore):
        self.usb=usb;self.store_factory=store_factory
        self.job=ExportJob(code_base,dict(preparing=self.prepare,copying=self.copy,
            verifying=self.verify,committed=self.ledger,ledger_committed=self.ready_to_purge,
            purging=self.purge))

    def create(self, items):
        if not isinstance(items,list) or not items:raise CloudError('没有可导出的采集批次')
        seen=set()
        for item in items:
            if (not isinstance(item,dict) or set(item)!={'mode','source','transaction_id'} or
                    item['mode'] not in ('emg','eeg') or not Path(item['source']).is_absolute() or
                    not isinstance(item['transaction_id'],str) or len(item['transaction_id'])!=32 or
                    any(c not in '0123456789abcdef' for c in item['transaction_id'])):
                raise CloudError('导出批次无效')
            key=(item['mode'],item['source'])
            if key in seen:raise CloudError('导出批次重复')
            seen.add(key)
        if len({item['transaction_id'] for item in items})!=len(items):raise CloudError('事务标识重复')
        self.usb.check()
        return self.job.create(dict(usb_uuid=self.usb.uuid,items=copy.deepcopy(items)))

    def _items(self,value):
        if value['identity'].get('usb_uuid')!=self.usb.uuid:raise CloudError('请重新接入原U盘')
        self.usb.check()
        return value['identity']['items']

    def _record(self,item,**fields):
        with self.job.lock:
            value=self.job.status();results=value.setdefault('results',{})
            results.setdefault(item['transaction_id'],{}).update(fields)
            atomic(self.job.path,value)

    def _receipt(self,item):
        value=self.job.status(); result=value.get('results',{}).get(item['transaction_id'],{})
        path=result.get('receipt')
        if not isinstance(path,str):raise CloudError('导出收据尚未持久化')
        expected=Path(result.get('base',''))/'runtime/usb_exports/receipts'/(item['transaction_id']+'.json')
        if Path(path)!=expected or expected.is_symlink():raise CloudError('导出收据路径不安全')
        receipt=read_json(path)
        if (not isinstance(receipt,dict) or receipt.get('source')!=item['source'] or
                receipt.get('usb_uuid')!=self.usb.uuid or receipt.get('state')!='verified'):
            raise CloudError('导出收据不匹配')
        return result, receipt

    def prepare(self,value,progress):
        for item in self._items(value):
            store=self.store_factory(item['mode'],item['source'])
            meta=store.metadata(item['source'])
            if (meta.get('id') != Path(item['source']).name or
                    meta.get('status') not in ('success','pending_qc','error','qc_error','failed','interrupted','cancelled')):
                raise CloudError('批次尚未确认结束，禁止导出')
            self._record(item,base=str(store.base),status=meta.get('status'),
                         qc_state=meta.get('qc_state'))

    def copy(self,value,progress):
        for item in self._items(value):
            result=self.job.status().get('results',{}).get(item['transaction_id'],{})
            base=result.get('base')
            if not isinstance(base,str):raise CloudError('采集模式上下文缺失')
            def report(phase,done,total):progress(done,total)
            path,receipt=copy_recording(base,item['source'],self.usb,report,transaction_id=item['transaction_id'])
            self._record(item,receipt=str(path),destination=receipt['destination'])

    def verify(self,value,progress):
        for item in self._items(value):
            result,receipt=self._receipt(item)
            _verify(self.usb,receipt['destination'],receipt['manifest'])
            check_source(item['source'],receipt['manifest'])

    def ledger(self,value,progress):
        for item in self._items(value):
            result,receipt=self._receipt(item)
            store=self.store_factory(item['mode'],item['source'])
            if str(store.base)!=result.get('base'):raise CloudError('采集模式上下文已变化')
            path=commit_credential(store.base,store,item['source'],Path(result['receipt']),self.usb)
            self._record(item,credential=str(path))

    def ready_to_purge(self,value,progress):
        for item in self._items(value):
            result,_=self._receipt(item)
            if not result.get('credential'):raise CloudError('导出台账凭据缺失')

    def purge(self,value,progress):
        for item in self._items(value):
            result,receipt=self._receipt(item)
            if result.get('status')=='pending_qc' or result.get('qc_state')=='pending':
                self._record(item,cleanup='retained_pending_qc');continue
            cleanup(result['base'],item['source'],receipt['destination'],receipt['manifest'],self.usb)
            self._record(item,cleanup='complete')

    def run(self):
        self.usb.check()
        return self.job.run()
