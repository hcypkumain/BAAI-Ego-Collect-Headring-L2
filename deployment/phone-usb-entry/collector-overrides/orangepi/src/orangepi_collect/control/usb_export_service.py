"""Controller-owned asynchronous export service and restart recovery."""
import threading
from pathlib import Path
from cloud_common import CloudError
from usb_export_discovery import discover
from usb_export_gate import pending_exports
from usb_export_jobs import ExportJob
from usb_export_lease import MountLease
from usb_export_pipeline import ExportPipeline

class ExportService:
    def __init__(self,code_base,lease_factory=MountLease,discover_fn=discover,pipeline_factory=ExportPipeline):
        self.base=Path(code_base);self.lease_factory=lease_factory;self.discover=discover_fn;self.pipeline_factory=pipeline_factory
        self.lock=threading.RLock();self.thread=None;self.error='';self.message='';self.unmounted=False
    def active(self):
        with self.lock:return bool(self.thread and self.thread.is_alive())
    def status(self):
        with self.lock:
            try:value=ExportJob(self.base,{}).status()
            except Exception as error:return dict(state='recovery',error=str(error),active=self.active())
            return dict(state=(value or {}).get('state','idle'),done_bytes=(value or {}).get('done_bytes',0),
                total_bytes=(value or {}).get('total_bytes',0),results=(value or {}).get('results',{}),
                error=self.error or (value or {}).get('error',''),active=self.active(),unmounted=self.unmounted)
    def start(self,recover=False):
        with self.lock:
            if self.active():raise CloudError('U盘导出已经运行')
            pending=pending_exports(self.base)
            known=str(self.base/'runtime/usb_exports/pending.json')
            if any(row.get('path')!=known for row in pending):raise CloudError('其他采集模式有未恢复的导出事务')
            if bool(pending)!=bool(recover):raise CloudError('导出事务状态已变化，请刷新')
            self.error='';self.unmounted=False
            def work():
                try:
                    with self.lease_factory() as usb:
                        pipeline=self.pipeline_factory(self.base,usb)
                        if not recover:
                            items=self.discover()
                            pipeline.create(items)
                        pipeline.run()
                    with self.lock:self.unmounted=True
                except Exception as error:
                    with self.lock:self.error=str(error)[:500]
            self.thread=threading.Thread(target=work,name='usb-export-service',daemon=True)
            self.thread.start()
