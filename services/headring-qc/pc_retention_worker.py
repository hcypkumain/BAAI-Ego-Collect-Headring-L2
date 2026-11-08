"""Expiry worker: only signed cloud-confirmed packages, never raw ingest age."""
import json
from pathlib import Path
import sqlite3
import time
from pc_retention import Retention,atomic

def related_jobs(app):
    path=app/'state/receiver/receiver.sqlite3'
    if not path.is_file():return []
    connection=sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True)
    rows=connection.execute('SELECT device,session,details,record FROM jobs').fetchall();connection.close()
    result=[]
    for device,session,details,record in rows:
        try:result.append(dict(json.loads(record),device=device,session=session,details=json.loads(details)))
        except (ValueError,TypeError):continue
    return result

def run_pass(app,config,now=None):
    from host_storage import require_storage
    app=Path(app);now=time.time() if now is None else now
    require_storage(config)
    state=Path(config.get('cloud_upload_state_root',str(app/'integrations/robot-uploader-1.0.0/adapter/cloud-jobs')))
    db=state/'cloud-upload.sqlite3';results=[]
    if not db.is_file():return {'checked':0,'deleted':0,'errors':[]}
    retention=Retention(app/'state/pc-retention')
    related=related_jobs(app)
    connection=sqlite3.connect('file:'+str(db)+'?mode=ro',uri=True)
    rows=connection.execute("SELECT target,receipt FROM uploads WHERE state='remote_confirmed' AND receipt IS NOT NULL").fetchall();connection.close()
    for target,raw in rows:
        try:
            receipt=json.loads(raw);retention.verify(receipt)
            if receipt['dataset']!=target:raise ValueError('Cloud package identity differs from queue')
            if now<receipt['pc_delete_after']:continue
            result=retention.cleanup(receipt,Path(config['data_root']),config['storage_uuid'],now=now)
            results.append(dict(target=target,**result))
            if result['state']=='deleted':
                from raw_handoff import verify_signature,RAW_PURPOSE,sha_value
                for job in related:
                    if job.get('cloud_dataset')!=target or job.get('cloud_state')!='remote_confirmed':continue
                    key=(app/'secrets'/job['device']).read_bytes();raw=job['raw_receipt'];archive=job['archive_receipt']
                    verify_signature(raw,key,RAW_PURPOSE);verify_signature(archive,key,'host_qc_archive_v1')
                    if archive.get('raw_handoff_receipt_sha256')!=sha_value(raw):raise ValueError('Related archive/raw identity differs')
                    root=Path(config['data_root']);day=job['details']['day'];device=job['device'];session=job['session']
                    paths=[('originals',root/'originals'/device/'received'/day/session,raw['manifest']),
                           ('archive',root/'archive'/day/device/session,archive['manifest'])]
                    lineage={'raw_handoff_receipt_sha256':sha_value(raw),'archive_manifest_digest':archive['manifest']['digest']}
                    for family,path,manifest in paths:
                        related_receipt=retention.related(receipt,path,manifest['files'],family,lineage)
                        results.append(dict(target=target,family=family,**retention.cleanup(related_receipt,root,config['storage_uuid'],now=now)))
        except Exception as error:results.append(dict(target=target,state='held',error=str(error)[:500]))
    summary={'checked':len(rows),'deleted':sum(r['state']=='deleted' for r in results),
             'errors':[r for r in results if 'error' in r],'results':results,'checked_at':now}
    atomic(app/'state/pc-retention/status.json',summary);return summary

def watch(app,config,stop):
    while not stop.is_set():
        try:run_pass(app,config)
        except Exception as error:print('PC_RETENTION_HOLD '+str(error)[:500],flush=True)
        stop.wait(60)
