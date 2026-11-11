import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'))
import project_paths
from daily_collection import DailyCollection, portions, take_snapshot, CST

class Store:
    def __init__(self,root):
        self.base=root/'orangepi';self.volume=root/'cp-test';self.root=self.volume/'data/shift'
        self.operations=self.base/'runtime/recording_operations';self.operations.mkdir(parents=True)
        self.root.mkdir(parents=True)
    def roots(self):return self.root,self.volume
    def metadata(self,path,ledger_cache=None):return json.loads((path/'meta.json').read_text())

class DailyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(Path(self.temp.name))
        self.now=dt.datetime(2026,11,4,12,tzinfo=CST).timestamp()
        self.state=dict(state='idle',privacy=dict(required=False),worker={})
        self.daily=DailyCollection(self.store.base,self.store,lambda:self.state,clock=lambda:self.now,autostart=False)
    def tearDown(self):self.daily.close();self.temp.cleanup()
    def make(self,name,level='INFO',kept=True,duration=60,start=None):
        p=self.store.root/name;p.mkdir()
        start=self.now-3600 if start is None else start
        meta=dict(created_unix=start,duration=duration,retained=kept,reference_attempt_succeeded=kept,
                  id=name,token='test-token',status='success' if kept else 'unconfirmed',qc_level=level,reference_name='擦眼镜',reference_path='a.png',
                  # This accounting unit fixture starts at the already-qualified
                  # source boundary; strict qualification is covered by the
                  # RecordingStore/device-contract integration tests.
                  qualified=bool(kept and level in ('INFO','WARN')))
        summary=dict(start_unix_ns=round(start*1e9),stop_unix_ns=round((start+duration)*1e9),
                     start_monotonic_ns=10**12,stop_monotonic_ns=10**12+round(duration*1e9),status='success',qc_level=level,emg_left=dict(emg_frames=10))
        report=dict(level=level,findings=[],streams={})
        if level=='ERROR':report['findings']=[dict(level='ERROR',check='RunWindow',message='bad')]
        if level=='WARN':report['streams']={'emg_left':dict(findings=[dict(level='WARN',check='Jitter',message='warn')])}
        for n,v in [('meta.json',meta),('capture_summary.json',summary),('qc_report.json',report)]:(p/n).write_text(json.dumps(v),encoding='utf-8')
        return p
    def test_only_confirmed_info_warn_count(self):
        self.make('pass');self.make('warn','WARN',duration=90);self.make('error','ERROR');self.make('pending',kept=False)
        self.daily.refresh();s=self.daily.snapshot()
        self.assertEqual((s['total_seconds'],s['qualified_count'],s['pass_count'],s['warn_count']),(150,2,1,1))
    def test_deduplicates_polling_and_survives_restart(self):
        self.make('one');self.daily.refresh();self.daily.refresh()
        restarted=DailyCollection(self.store.base,self.store,lambda:self.state,clock=lambda:self.now,autostart=False)
        self.assertEqual(restarted.snapshot()['total_seconds'],60)
    def test_midnight_splits_actual_time(self):
        start=dt.datetime(2026,11,3,23,59,30,tzinfo=CST).timestamp();self.make('cross',duration=90,start=start)
        self.daily.refresh();self.assertEqual(self.daily.snapshot('2026-11-03')['total_seconds'],30)
        self.assertEqual(self.daily.snapshot()['total_seconds'],60)
    def test_monotonic_duration_ignores_wall_clock_adjustment(self):
        p=self.make('clock');s=json.loads((p/'capture_summary.json').read_text());s['stop_unix_ns']+=10**12
        (p/'capture_summary.json').write_text(json.dumps(s));self.assertEqual(take_snapshot(self.store,p)['duration'],60)
    def test_report_errors_override_success_metadata(self):
        p=self.make('one');(p/'qc_report.json').write_text(json.dumps(dict(level='ERROR',findings=[dict(level='ERROR',check='Timing')],streams={})))
        self.daily.refresh();self.assertEqual(self.daily.snapshot()['total_seconds'],0)
    def test_missing_qc_excluded_and_marked_in_report(self):
        p=self.make('one');(p/'qc_report.json').unlink();self.daily.refresh()
        self.assertEqual(self.daily.snapshot()['total_seconds'],0);self.assertIn('有数据但缺 QC 报告 1 条',self.daily.text())
    def test_deleted_take_not_counted(self):
        p=self.make('one');self.daily.refresh();(p/'recording_deleted.json').write_text('{}')
        meta=json.loads((p/'meta.json').read_text());meta.update(retained=False,status='deleted',reference_attempt_succeeded=False)
        (p/'meta.json').write_text(json.dumps(meta));self.daily.refresh();self.assertEqual(self.daily.snapshot()['total_seconds'],0)
    def test_verified_cloud_cleanup_preserves_time(self):
        p=self.make('one','WARN');self.daily.refresh()
        import shutil;shutil.rmtree(p)
        cloud=self.store.base/'runtime/cloud_uploads';(cloud/'jobs/job').mkdir(parents=True)
        (cloud/'queue.json').write_text(json.dumps(dict(jobs=[dict(id='job',session_id='one',local_deleted=True)])))
        (cloud/'jobs/job/receipt.json').write_text(json.dumps(dict(platform_completion_acknowledged=True)))
        self.daily.refresh();self.assertEqual(self.daily.snapshot()['total_seconds'],60)
    def test_missing_source_without_receipt_excluded(self):
        p=self.make('one');self.daily.refresh()
        import shutil;shutil.rmtree(p);self.daily.refresh();self.assertEqual(self.daily.snapshot()['total_seconds'],0)
    def test_discard_journal_preserves_attempt_but_excludes_duration(self):
        p=self.make('one');row=take_snapshot(self.store,p)
        import shutil;shutil.rmtree(p)
        (self.store.operations/'op.json').write_text(json.dumps(dict(kind='discard_recording',session_id='one',daily_snapshot=row)))
        self.daily.refresh();self.assertEqual(self.daily.snapshot()['total_seconds'],0);self.assertIn('重采/放弃',self.daily.text())
    def test_text_export_matches_totals_and_findings(self):
        self.make('pass');self.make('warn','WARN');self.make('fail','ERROR')
        target,text=self.daily.export();self.assertTrue(target.read_bytes().startswith(b'\xef\xbb\xbf'))
        self.assertIn('合格可推送录制：2 条',text);self.assertIn('120.0 秒',text);self.assertIn('Jitter',text);self.assertIn('RunWindow',text)
    def test_busy_export_does_not_read_or_write(self):
        self.state['state']='recording'
        with self.assertRaises(ValueError):self.daily.export()
        self.assertFalse(self.daily.path.exists())
    def test_day_rollover_and_invalid_dates(self):
        self.make('one');self.daily.refresh();self.now+=86400;self.assertEqual(self.daily.snapshot()['total_seconds'],0)
        with self.assertRaises(ValueError):self.daily.snapshot('../../bad')
    def pending(self,name='pending',duration=60,start=None):
        p=self.make(name,duration=duration,start=start)
        for filename in ['meta.json','capture_summary.json']:
            data=json.loads((p/filename).read_text())
            data.update(status='pending_qc',qc_level=None,qc_state='pending',reference_attempt_succeeded=False)
            (p/filename).write_text(json.dumps(data))
        (p/'qc_report.json').unlink()
        return p
    def test_saved_pending_counts_without_qualifying(self):
        self.pending();self.daily.refresh();s=self.daily.snapshot()
        self.assertEqual((s['saved_seconds'],s['saved_count'],s['pending_qc_seconds'],s['pending_qc_count']),(60,1,60,1))
        self.assertEqual((s['total_seconds'],s['qualified_count']),(0,0))
        self.assertIn('已保存/待主机质检',self.daily.text())
    def test_pending_midnight_and_restart(self):
        start=dt.datetime(2026,11,3,23,59,30,tzinfo=CST).timestamp()
        self.pending(duration=90,start=start);self.daily.refresh()
        self.assertEqual(self.daily.snapshot('2026-11-03')['saved_seconds'],30)
        restarted=DailyCollection(self.store.base,self.store,lambda:self.state,clock=lambda:self.now,autostart=False)
        self.assertEqual(restarted.snapshot()['pending_qc_seconds'],60)
    def test_qc_completion_does_not_double_count_saved(self):
        p=self.pending();self.daily.refresh()
        meta=json.loads((p/'meta.json').read_text());meta.update(status='success',reference_attempt_succeeded=True,qc_level='WARN')
        meta['qualified']=True
        summary=json.loads((p/'capture_summary.json').read_text());summary.update(status='success',qc_level='WARN')
        for name,value in [('meta.json',meta),('capture_summary.json',summary),('qc_report.json',dict(level='WARN'))]:
            (p/name).write_text(json.dumps(value))
        self.daily.refresh();s=self.daily.snapshot()
        self.assertEqual((s['saved_seconds'],s['total_seconds'],s['pending_qc_seconds']),(60,60,0))
    def test_failed_kept_is_saved_but_never_qualified(self):
        self.make('kept-error',level='ERROR');self.daily.refresh();s=self.daily.snapshot()
        self.assertEqual((s['saved_seconds'],s['saved_unqualified_seconds'],s['total_seconds']),(60,60,0))
    def test_unconfirmed_empty_deleted_and_missing_excluded_from_saved(self):
        self.make('unconfirmed',kept=False);p=self.pending();self.make('empty',duration=0)
        self.daily.refresh();self.assertEqual(self.daily.snapshot()['saved_seconds'],60)
        import shutil;shutil.rmtree(p)
        self.daily.refresh();self.assertEqual(self.daily.snapshot()['saved_seconds'],0)
    def test_legacy_cache_immediately_counts_pending(self):
        p=self.pending();self.daily.records={'pending':take_snapshot(self.store,p)};self.daily.updated=self.now
        self.assertEqual(self.daily.snapshot()['saved_seconds'],60)
    def test_ready_refreshes_but_active_recording_does_not(self):
        self.pending();self.state['state']='ready';self.assertTrue(self.daily.refresh())
        self.assertEqual(self.daily.snapshot()['saved_seconds'],60)
        self.pending('new');self.state['worker']['recording_active']=True
        self.assertFalse(self.daily.refresh());self.assertEqual(self.daily.snapshot()['saved_seconds'],60)
        self.state['worker']={};self.state['privacy']['required']=True
        self.assertFalse(self.daily.refresh())
        self.state['privacy']['required']=False;self.state['state']='recording'
        self.assertFalse(self.daily.refresh())
    def test_unreadable_metadata_never_keeps_old_saved_time(self):
        p=self.pending();self.daily.refresh();(p/'meta.json').write_text('broken')
        self.daily.refresh();self.assertEqual(self.daily.snapshot()['saved_seconds'],0)
    def test_enqueue_saves_report_before_queueing_without_upload(self):
        from cloud_uploads import UploadManager
        self.make('one');self.store.path=lambda identifier:self.store.root/identifier
        manager=UploadManager(self.store.base,self.store,lambda:self.state,autostart=False)
        manager.daily_collection=self.daily
        try:
            manager.set_credentials(dict(ak='test-ak',sk='test-sk'))
            result=manager.enqueue(dict(selection=[dict(session_id='one',token='test-token')],settings=dict(project_name='EGOL2',target_parent='EGOL2/test',task_id=None)))
            self.assertEqual(len(result['queued']),1)
            job=manager.state['jobs'][0]
            self.assertEqual(job['state'],'queued');self.assertTrue(Path(job['daily_report']).is_file())
            self.assertIsNone(manager.thread)
        finally:manager.close()

if __name__=='__main__':unittest.main()
