"""Hold-mode history built only from NVMe ledgers and precomputed evidence."""
import csv
import json
import sqlite3
import time
from pathlib import Path

from history import History, day_of, session_time, safe_json, ro, connection
import sys

DAY = '2026-11-09'
APP = Path(__file__).resolve().parents[1]
# Public source archive intentionally excludes the private recovery workspace.
# This legacy adapter is not wired into the v1 fixture path.
WORKSPACE = APP / 'private-recovery-evidence-not-archived'
sys.path.insert(0, str(WORKSPACE / 'work/recovery-service'))
from raw_handoff import RAW_PURPOSE, sha_value, verify_signature
OLD_DB = APP / 'state/receiver/receiver.sqlite3'
NVME_DB = WORKSPACE / 'work/recovery-service/state/receiver/receiver.sqlite3'
INTERNAL_EVIDENCE = WORKSPACE / 'outputs/2026-11-09逐段接收质检明细.csv'
EXTERNAL_SESSIONS = WORKSPACE / 'outputs/2026-11-09外置盘144段核验汇总.csv'
EXTERNAL_FILE_REPORT = WORKSPACE / 'outputs/2026-11-09外置盘144段逐文件核验.csv'
BACKUP_STATE = WORKSPACE / 'work/external-144-backup-state.json'
KEYS = WORKSPACE / 'work/recovery-service/secrets'


class RecoveryHistory(History):
    """History API compatible with the old monitor, with all media reads bypassed."""
    def __init__(self, app, state):
        Path(state).mkdir(parents=True, exist_ok=True)
        super().__init__(app, state)
        self.receiver_items = []
        self.external_verified = set()
        self.external_status = {}
        self.internal_evidence = {}
        self.backup_complete = False
        self.backup_state_valid = False
        self._external_file_signature = None
        self._external_file_index = {}
        self.write_hold = {
            'ready': False,
            'error': '外置存储处于只读核验维护；接收、释放、回写和上传已停用',
            'uuid': None,  # Private installation identity is not source evidence.
            'readonly': True,
        }

    @staticmethod
    def _rows(path):
        connection = sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True, timeout=3)
        try:
            return connection.execute(
                "SELECT device,session,details,record FROM jobs WHERE session LIKE ?",
                ('%' + DAY + '%',),
            ).fetchall()
        finally:
            connection.close()

    @staticmethod
    def _csv(path):
        if not path.is_file():
            return {}
        with path.open(newline='', encoding='utf-8-sig') as handle:
            return {(row.get('设备'), row.get('段')): row for row in csv.DictReader(handle)
                    if row.get('设备') and row.get('段')}

    def _external_results(self):
        self.external_status = self._csv(EXTERNAL_SESSIONS)
        try:
            value = json.loads(BACKUP_STATE.read_text())
            self.backup_state_valid = (value.get('status') == 'complete'
                                       and value.get('verified_sessions') == 144
                                       and value.get('target_hash_passed') == value.get('original_files', 0) + value.get('qc_files', 0))
        except (OSError, ValueError, TypeError):
            self.backup_state_valid = False

    def _external_file_rows(self):
        try:
            stat = EXTERNAL_FILE_REPORT.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return {}
        if signature == self._external_file_signature:
            return self._external_file_index
        grouped = {}
        with EXTERNAL_FILE_REPORT.open(newline='', encoding='utf-8-sig') as handle:
            for row in csv.DictReader(handle):
                key=(row.get('设备'),row.get('段'))
                if key[0] and key[1]:grouped.setdefault(key,[]).append(row)
        self._external_file_signature = signature
        self._external_file_index = grouped
        return grouped

    def sync(self):
        errors = []
        try:
            internal = self._csv(INTERNAL_EVIDENCE)
            self._external_results()
            external_files = self._external_file_rows()
            nvme = self._rows(NVME_DB)
            old = self._rows(OLD_DB)
            records = {}

            for device, session, details_json, record_json in nvme:
                if device not in ('cp_0007', 'cp_0011', 'cp_0013'):
                    continue
                details, record = safe_json(details_json), safe_json(record_json)
                evidence = internal.get((device, session), {})
                native = details.get('native_sha256')
                raw = record.get('raw_receipt') or {}
                archive = record.get('archive_receipt') or {}
                result = record.get('result') or {}
                key_path = KEYS / device
                try:
                    key = key_path.read_bytes()
                    raw_digest = sha_value(raw)
                    verify_signature(raw, key, RAW_PURPOSE)
                    verify_signature(archive, key, 'host_qc_archive_v1')
                    proof_ok = (record.get('receive_state') == 'host_committed'
                                and raw.get('device_id') == device and raw.get('session_id') == session
                                and raw.get('host_storage_uuid') == '9111f68c-41cb-4e77-ad99-a2ff3e9d89ea'
                                and raw.get('host_verified') is True and raw.get('host_fsynced') is True
                                and record.get('raw_receipt_sha256') == raw_digest
                                and archive.get('raw_handoff_receipt_sha256') == raw_digest
                                and raw.get('qc_request_id') == details.get('request_id')
                                and raw.get('manifest',{}).get('native_sha256') == native
                                and archive.get('manifest',{}).get('native_sha256') == native
                                and result.get('session_id') == session and result.get('request_id') == details.get('request_id')
                                and result.get('native_sha256') == native and record.get('result_sha256') == archive.get('result_sha256'))
                except (OSError, ValueError, TypeError):
                    proof_ok = False
                evidence_ok = (evidence.get('内盘证据核验') == '收据、原始及归档文件大小和全部QC附件摘要通过'
                               and evidence.get('采集摘要') == native)
                verified = bool(proof_ok and evidence_ok and record.get('process_state') == 'complete')
                level = record.get('level') if verified else None
                integrity = '内置盘收据、native与QC附件证据已核验' if verified else '内置盘身份或证据待复核'
                records[(device, session)] = self._history_row(
                    device, session, details, record, source='nvme-recovery', verified=verified,
                    integrity=integrity, level=level, backup='NVMe原始副本保留', native=native)

            # Compare overlaps from both ledgers by identity and native digest.
            old_map = {}
            for device, session, details_json, record_json in old:
                if device not in ('cp_0013', 'cp_0019'):
                    continue
                details, record = safe_json(details_json), safe_json(record_json)
                old_map[(device, session)] = (details, record)
                existing = records.get((device, session))
                if existing is not None:
                    if existing.get('native_sha256') != details.get('native_sha256'):
                        existing.update(verified=False, local_complete=False, level=None, pc_qc_level=None,
                                        outcome=None, integrity_status='NVMe与旧账本同identity但native摘要冲突',
                                        source='identity-conflict')

            for (device, session), (details, record) in old_map.items():
                if record.get('release_state') != 'deleted' or (device, session) in records:
                    continue
                status = self.external_status.get((device, session), {})
                native = details.get('native_sha256')
                raw = record.get('raw_receipt') or {}
                archive = record.get('archive_receipt') or {}
                result = record.get('result') or {}
                key_path = KEYS / device
                try:
                    key = key_path.read_bytes()
                    raw_digest = sha_value(raw)
                    verify_signature(raw, key, RAW_PURPOSE)
                    verify_signature(archive, key, 'host_qc_archive_v1')
                    release_ack = record.get('release_ack') or {}
                    receipt_ok = (raw_digest == record.get('raw_receipt_sha256')
                                  and raw_digest == archive.get('raw_handoff_receipt_sha256')
                                  and release_ack.get('state') == 'deleted'
                                  and release_ack.get('receipt') == raw
                                  and release_ack.get('receipt_signature') == raw.get('signature')
                                  and release_ack.get('receipt_sha256') == raw_digest
                                  and release_ack.get('device_id') == device and release_ack.get('session_id') == session
                                  and release_ack.get('native_sha256') == native
                                  and raw.get('device_id') == device and raw.get('session_id') == session
                                  and raw.get('server_id') == 'dc25b35b563d4d52b1f838e5bf2b1f60'
                                  and archive.get('device_id') == device and archive.get('session_id') == session
                                  and archive.get('server_id') == 'dc25b35b563d4d52b1f838e5bf2b1f60'
                                  and raw.get('qc_request_id') == details.get('request_id')
                                  and raw.get('manifest',{}).get('native_sha256') == native
                                  and archive.get('manifest',{}).get('native_sha256') == native
                                  and result.get('session_id') == session and result.get('request_id') == details.get('request_id')
                                  and result.get('native_sha256') == native and record.get('result_sha256') == archive.get('result_sha256'))
                except (OSError, ValueError, TypeError):
                    receipt_ok = False
                report_rows = external_files.get((device,session), [])
                raw_rows = [r for r in report_rows if r.get('类型') == '原始']
                archive_rows = [r for r in report_rows if r.get('类型') == '归档']
                qc_rows = [r for r in report_rows if r.get('类型') == 'QC结果/附件']
                native_rows = [r for r in raw_rows if r.get('相对路径') == 'native_capture_summary.json']
                archive_native_rows = [r for r in archive_rows if r.get('相对路径') == 'native_capture_summary.json']
                qc_by_path = {r.get('相对路径'): r for r in qc_rows}
                artifact_hashes = result.get('artifacts') or {}
                qc_files_ok = (len(qc_rows) == len(artifact_hashes) + 1
                               and qc_by_path.get('result.json',{}).get('期望SHA256') == record.get('result_sha256')
                               and qc_by_path.get('result.json',{}).get('实测SHA256') == record.get('result_sha256')
                               and qc_by_path.get('result.json',{}).get('状态') == '通过'
                               and all(qc_by_path.get('artifacts/'+name,{}).get('期望SHA256') == digest
                                       and qc_by_path.get('artifacts/'+name,{}).get('实测SHA256') == digest
                                       and qc_by_path.get('artifacts/'+name,{}).get('状态') == '通过'
                                       for name,digest in artifact_hashes.items()))
                file_rows_ok = (len(raw_rows) == int(status.get('原始manifest文件数') or -1)
                                and len(archive_rows) == int(status.get('归档manifest文件数') or -1)
                                and all(r.get('状态') == '通过' for r in raw_rows+archive_rows)
                                and len(native_rows)==1 and len(archive_native_rows)==1
                                and native_rows[0].get('期望SHA256') == native
                                and native_rows[0].get('实测SHA256') == native
                                and archive_native_rows[0].get('期望SHA256') == native
                                and archive_native_rows[0].get('实测SHA256') == native)
                verified_csv = (status.get('状态') == '证据核验通过'
                                and status.get('收据签名') == '通过'
                                and status.get('身份/native') == '通过'
                                and status.get('采集native_sha256') == native
                                and int(status.get('原始SHA通过数') or -1) == int(status.get('原始manifest文件数') or -2)
                                and int(status.get('归档SHA通过数') or -1) == int(status.get('归档manifest文件数') or -2)
                                and int(status.get('QC附件SHA通过数') or -1) == int(status.get('QC附件数') or -2)
                                and int(status.get('QC结果SHA通过') or 0) == 1
                                and file_rows_ok and qc_files_ok)
                verified = bool(receipt_ok and verified_csv and record.get('process_state') == 'complete')
                level = record.get('level') if verified else None
                integrity = ('外置原始、归档及QC附件逐文件SHA256/native通过' if verified
                             else '外置native、签名或文件证据不一致' if not receipt_ok or status.get('采集native_sha256') not in (None, native)
                             else '外置原件待核验' if not status else '外置核验有缺口')
                backup = ('NVMe备份完成' if self.backup_complete else '等待NVMe安全备份')
                records[(device, session)] = self._history_row(
                    device, session, details, record, source='external-legacy', verified=verified,
                    integrity=integrity, level=level, backup=backup, native=native)

            # Current-day capture files are intentionally unindexed in hold mode.
            self.records_today_indexed = False

            self.external_verified = {key for key, row in records.items()
                                      if row.get('source') == 'external-legacy' and row.get('verified')}
            self.backup_complete = self.backup_state_valid and len(self.external_verified) == 144
            for key, row in records.items():
                if row.get('source') == 'external-legacy':
                    row['backup_status'] = 'NVMe备份完成' if self.backup_complete else '等待NVMe安全备份'

            # A missing NVMe copy must not be confused with a complete receipt.
            self.receiver_items = sorted(
                (self._receiver_row(key, value) for key, value in records.items()),
                key=lambda row: float(row.get('created') or 0), reverse=True)
            with self.lock:
                self.records.clear()
                with connection(self.path, timeout=3) as c:
                    c.execute('DELETE FROM captures')
                for row in records.values():
                    super().upsert(row)
            self.ready = True
            self.error = None
            self.last_success = time.time()
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
            errors.append(type(exc).__name__)
            self.ready = False
            self.error = '只读台账暂不可用：' + ';'.join(errors)

    @staticmethod
    def _history_row(device, session, details, record, *, source, verified, integrity, level, backup, native=None):
        created = session_time(session)
        if created is None:
            created = float(record.get('created') or 0)
        return {
            'device': device, 'session': session, 'mode': 'eeg' if session.startswith('L3-') else 'emg',
            'task_name': details.get('task_name') or '未记录任务名称',
            'image_name': details.get('reference_name') or '',
            'image_key': details.get('reference_sha256') or details.get('reference_name') or '',
            'created': created, 'day': day_of(created), 'outcome': 'error' if level == 'ERROR' else ('success' if level else None),
            'saved': True, 'level': level, 'pc_qc_level': level, 'native_sha256': native or details.get('native_sha256'),
            'local_complete': bool(verified), 'verified': bool(verified), 'integrity_status': integrity,
            'backup_status': backup, 'source': source,
            'process_state': record.get('process_state'), 'release_state': record.get('release_state'),
            'receive_state': record.get('receive_state'), 'reply_state': record.get('reply_state'),
            'standardized': record.get('standard_state') in ('complete', 'published'),
            'cloud_state': record.get('cloud_state'), 'removed': record.get('process_state') == 'owner_removed',
            'receive_error': record.get('receive_error'), 'release_error': record.get('release_error'),
            'process_error': record.get('process_error'), 'reply_error': record.get('reply_error'),
            'bytes': details.get('bytes', 0),
        }

    def _receiver_row(self, key, record):
        external = record.get('source') == 'external-legacy'
        verified = bool(record.get('verified'))
        if external and not verified:
            process_state = 'legacy_pending_verification'
            process_error = record.get('integrity_status') or '外置原件待核验'
        elif external and verified:
            process_state = 'legacy_verified_external'
            process_error = '原始、归档及QC附件哈希核验通过；外盘保持只读'
        else:
            process_state = 'complete' if verified else 'legacy_pending_verification'
            process_error = None if verified else record.get('integrity_status')
        return {
            'device': key[0], 'session': key[1], 'reference_name': record.get('image_name'),
            'bytes': record.get('bytes', 0), 'receive_state': 'host_committed' if verified else 'legacy_pending_verification',
            'release_state': record.get('release_state') if external else 'retained',
            'process_state': process_state, 'reply_state': record.get('reply_state') if external else 'held_pi_original',
            'standard_state': 'complete' if record.get('standardized') else 'not_requested',
            'level': record.get('pc_qc_level'), 'cloud_state': record.get('cloud_state'),
            'pc_delete_after': None, 'receive_error': None, 'release_error': None,
            'process_error': process_error, 'reply_error': None,
            'integrity_status': record.get('integrity_status'), 'backup_status': record.get('backup_status'),
            'created': record.get('created'), 'verified': verified,
        }

    def summaries(self):
        counts = super().summaries()
        for value in counts.values():
            value['today_saved'] = None
            value['today_local_complete'] = None
            value['today_pc_qc_done'] = None
            value['today_pc_qc_error'] = None
            value['today_indexed'] = False
        return counts

    def receiver(self, page=1, limit=20, device=None):
        items = [row for row in self.receiver_items if not device or row['device'] == device]
        page_items = items[(page - 1) * limit:page * limit]
        verified_external = sum(1 for row in items if row['device'] in ('cp_0013', 'cp_0019')
                                and row.get('verified') and row['release_state'] == 'deleted')
        return {
            'control': {'receiving': False, 'stopping': False, 'revision': 0,
                        'updated': time.time(), 'write_enabled': False, 'readonly': True,
                        'note': '外置存储只读核验维护中；接收和释放均已停用'},
            'storage': dict(self.write_hold),
            'summary': {'total': len(items), 'received': sum(bool(r.get('verified')) for r in items),
                        'released': sum(r.get('release_state') == 'deleted' for r in items),
                        'processed': sum(r.get('level') in ('INFO', 'WARN', 'ERROR') for r in items),
                        'pc_qc_error': sum(r.get('level') == 'ERROR' for r in items),
                        'cloud': 0, 'external_pending': sum(r['device'] in ('cp_0013','cp_0019') and not r.get('verified') for r in items),
                        'external_verified': verified_external, 'day': DAY,
                        'today_indexed': False},
            'items': page_items, 'total': len(items), 'page': page, 'limit': limit,
            'error': self.error, 'history_ready': self.ready,
        }
