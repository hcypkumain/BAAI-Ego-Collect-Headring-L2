"""Durable daily collection accounting and text reports; never opens devices."""
import copy
import datetime as dt
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
import threading
import time
from recording_store import atomic_json, bounded, CST
from qc_details import summarize_qc

SAFE = frozenset(('device_confirm', 'idle', 'failed_wait', 'exhausted', 'confirm_stop'))
REFRESH_SAFE = SAFE | frozenset(('ready',))


def load(path):
    try:
        value = json.loads(bounded(path, 8 * 1024 * 1024))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def day_name(stamp):
    return dt.datetime.fromtimestamp(stamp, CST).date().isoformat()


def take_snapshot(store, path, ledger_cache=None, metadata=None):
    """Keep small factual audit evidence even when a discarded take is purged."""
    path = Path(path)
    meta = metadata if metadata is not None else (store.metadata(path) if ledger_cache is None else store.metadata(path, ledger_cache=ledger_cache))
    summary = load(path / 'capture_summary.json')
    report = load(path / 'qc_report.json')
    start = summary.get('start_unix_ns', 0)
    start = start / 1e9 if number(start) and start > 0 else meta['created_unix']
    duration = 0
    for left, right in (('start_monotonic_ns', 'stop_monotonic_ns'), ('start_unix_ns', 'stop_unix_ns')):
        a, b = summary.get(left), summary.get(right)
        if number(a) and number(b) and a > 0 and b >= a:
            duration = (b - a) / 1e9
            break
    if not duration and number(meta.get('duration')):
        duration = max(0, meta['duration'])
    qc = summarize_qc(report)
    levels = [str(v).upper() for v in (report.get('level'), summary.get('qc_level'), meta.get('qc_level')) if v]
    level = 'ERROR' if qc['error_count'] or 'ERROR' in levels else 'WARN' if 'WARN' in levels else 'INFO' if 'INFO' in levels else None
    counts = ('frames', 'emg_frames', 'samples', 'samples_saved', 'samples_written', 'frames_saved')
    has_data = any(isinstance(v, dict) and any(number(v.get(k)) and v[k] > 0 for k in counts) for v in summary.values())
    if not has_data:
        has_data = any(p.is_file() and not p.is_symlink() and p.stat().st_size > 0 for pattern in ('*/*.npz', '*/*.mp4', '*/*.h264', '*/*.mcap') for p in path.glob(pattern))
    qualified = bool(meta.get('qualified') is True and meta.get('retained') and meta.get('reference_attempt_succeeded') and
                     report.get('scope','full')=='full' and
                     summary.get('status') == 'success' and report.get('level') in ('INFO', 'WARN') and
                     level in ('INFO', 'WARN') and has_data and duration > 0)
    findings = [dict(level=f['level'], check=f['check'] or '未标检查项', stream=f['modality']) for f in qc['findings']]
    return dict(id=path.name, start=start, day=day_name(start), duration=duration,
                reference=meta.get('reference_name') or '-', reference_path=meta.get('reference_path'),
                status=meta['status'], retained=meta['retained'], qualified=qualified, accepted_at_capture=qualified,
                has_data=has_data, qc_done=bool(report.get('level') in ('INFO', 'WARN', 'ERROR')),
                qc_level=level, errors=qc['error_count'], warnings=qc['warn_count'],
                findings=findings, uploaded=False, local_cleaned=False)


def saved_recording(row):
    """Saved acquisition time is independent of a later host QC verdict."""
    return bool(row.get('retained') and row.get('has_data') and
                row.get('status') not in ('deleted', 'discarded', 'cancelled', 'unconfirmed') and
                not row.get('read_error') and not row.get('source_missing'))


def portions(row, include_saved=False):
    """Split elapsed recording time at Beijing midnight, independent of host TZ."""
    if not (saved_recording(row) if include_saved else row.get('qualified')):
        return {}
    start, seconds = row.get('start'), row.get('duration')
    if not number(start) or not number(seconds) or seconds <= 0:
        return {}
    end = start + seconds
    result = {}
    while start < end:
        current = dt.datetime.fromtimestamp(start, CST)
        boundary = dt.datetime.combine(current.date() + dt.timedelta(days=1), dt.time(), CST).timestamp()
        stop = min(end, boundary)
        result[current.date().isoformat()] = stop - start
        start = stop
    return result


class DailyCollection:
    def __init__(self, base, store, display_state, clock=time.time, autostart=True):
        self.base, self.store, self.display_state, self.clock = Path(base), store, display_state, clock
        self.path = self.base / 'runtime/daily_collection/records.json'
        saved = load(self.path)
        self.records = saved.get('records', {})
        self.updated = saved.get('updated_at')
        self.error = None
        self.lock = threading.RLock()
        self.refresh_lock = threading.Lock()
        self.refresh_generation = 0
        self.export_lock = threading.Lock()
        self.ledger_cache = {}
        self.file_cache = {}
        self.stop = threading.Event()
        self.thread = None
        if autostart:
            self.thread = threading.Thread(target=self._run, name='daily-collection', daemon=True)
            self.thread.start()

    def busy(self, refresh=False):
        state = self.display_state()
        return (state.get('state') not in (REFRESH_SAFE if refresh else SAFE) or (state.get('privacy') or {}).get('required') or
                (state.get('worker') or {}).get('recording_active') is True)

    def _run(self):
        while not self.stop.is_set():
            try:
                if not self.busy(refresh=True):
                    self.refresh()
            except Exception as error:
                with self.lock:
                    self.error = str(error)[:200]
            self.stop.wait(5)

    def refresh(self, wait=False):
        if self.busy(refresh=True):
            return False
        with self.lock:
            generation = self.refresh_generation
        # Exports can join a running refresh instead of failing immediately.
        # Background refreshes remain non-blocking; bound request latency.
        if not self.refresh_lock.acquire(timeout=20 if wait else 0):
            return False
        try:
            if self.busy(refresh=True):
                return False
            with self.lock:
                if wait and self.refresh_generation != generation:
                    return True
            _, volume = self.store.roots()
            with self.lock:
                records = copy.deepcopy(self.records)
            live = set()
            project = getattr(self.store, 'project', None)
            ledger_signature = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size)
                                     for p in (project / 'configs/environments').rglob('used.yaml')) if project else ()
            # All shifts, including previous days; no raw signal/video contents read.
            paths = list((volume / 'data').glob('*/*')) + list((volume / 'deleted-recordings').glob('*'))
            for path in paths:
                if self.busy(refresh=True):
                    return False
                if path.is_symlink() or not path.is_dir():
                    continue
                if not (path / 'meta.yaml').is_file() and not (path / 'meta.json').is_file():
                    continue
                # Deleted directories have a suffix assigned by the delete journal.
                marker = load(path / 'recording_deleted.json')
                identifier = path.name
                if marker.get('operation_id'):
                    operation = load(self.store.operations / (marker['operation_id'] + '.json'))
                    identifier = operation.get('session_id', identifier)
                live.add(identifier)
                try:
                    signature = [ledger_signature]
                    # RecordingStore's token includes frozen selection/QC
                    # evidence plus required-stream filesystem identities.
                    # Invalidate cached qualification if any proof changes.
                    metadata=self.store.metadata(path,ledger_cache=self.ledger_cache)
                    signature.append(('capture_proof',metadata.get('token')))
                    for name in ('meta.yaml', 'meta.json', 'capture_summary.json', 'qc_report.json', 'recording_deleted.json'):
                        source = path / name
                        try:
                            info = source.stat()
                            signature.append((info.st_mtime_ns, info.st_size))
                        except FileNotFoundError:
                            signature.append(None)
                    cached = self.file_cache.get(str(path))
                    if cached is not None and cached[0] == signature:
                        row = copy.deepcopy(cached[1])
                    else:
                        row = take_snapshot(self.store, path, self.ledger_cache, metadata=metadata)
                        self.file_cache[str(path)] = (signature, copy.deepcopy(row))
                    row['id'] = identifier
                    if marker:
                        row.update(status='deleted', retained=False, qualified=False)
                    records[identifier] = row
                except (OSError, ValueError, TypeError, KeyError) as error:
                    # A changing or broken take must never retain an old success.
                    if identifier in records:
                        records[identifier].update(qualified=False, read_error=str(error)[:160])
            for journal in self.store.operations.glob('*.json'):
                operation = load(journal)
                if operation.get('kind') != 'discard_recording':
                    continue
                identifier = operation.get('session_id')
                row = operation.get('daily_snapshot') or records.get(identifier)
                if row:
                    records[identifier] = dict(row, status='discarded', retained=False, qualified=False)
                    live.add(identifier)
            # Verified cloud cleanups keep the existing audit record and its time.
            queue = load(self.base / 'runtime/cloud_uploads/queue.json')
            for job in queue.get('jobs', []):
                identifier = job.get('session_id')
                receipt = load(self.base / 'runtime/cloud_uploads/jobs' / str(job.get('id', '')) / 'receipt.json')
                if identifier in records and receipt.get('platform_completion_acknowledged'):
                    records[identifier]['uploaded'] = True
                    if job.get('local_deleted'):
                        records[identifier]['local_cleaned'] = True
                        records[identifier]['qualified'] = bool(records[identifier].get('accepted_at_capture'))
                        records[identifier].pop('source_missing', None)
                        live.add(identifier)
            from usb_export_ledger import completed_credential
            for credential in (self.base/'runtime/usb_exports/credentials').glob('*.json'):
                try:
                    from usb_export_ledger import _read
                    raw = _read(credential)
                    proof = completed_credential(self.base, raw['source'])
                    row = proof.get('daily_snapshot')
                    if not isinstance(row,dict) or row.get('id') != Path(proof['source']).name:continue
                    identifier = row['id']
                    if identifier in live:continue
                    row = copy.deepcopy(row)
                    row['local_cleaned'] = True
                    row['usb_exported'] = True
                    row.pop('source_missing',None)
                    records[identifier] = row
                    live.add(identifier)
                except (OSError,ValueError,TypeError,KeyError,RecursionError):continue
            for identifier, row in records.items():
                if identifier not in live and not row.get('local_cleaned'):
                    row['qualified'] = False
                    row['source_missing'] = True
            if self.busy(refresh=True):
                return False
            updated = self.clock()
            with self.lock:
                changed = records != self.records or not self.path.is_file()
                if changed:
                    atomic_json(self.path, dict(version=1, timezone='Asia/Shanghai', updated_at=updated, records=records))
                self.records, self.updated, self.error = records, updated, None
                self.refresh_generation += 1
            return True
        finally:
            self.refresh_lock.release()

    def snapshot(self, day='today'):
        if day == 'today':
            day = day_name(self.clock())
        dt.date.fromisoformat(day)
        with self.lock:
            rows = copy.deepcopy(list(self.records.values()))
            updated, error = self.updated, self.error
        rows = [r for r in rows if r.get('day') == day or day in portions(r) or day in portions(r, include_saved=True)]
        qualified = [r for r in rows if r.get('qualified') and portions(r).get(day, 0) > 0]
        saved = [r for r in rows if portions(r, include_saved=True).get(day, 0) > 0]
        pending = [r for r in saved if r.get('status') == 'pending_qc' and not r.get('qualified')]
        other = [r for r in saved if not r.get('qualified') and r.get('status') != 'pending_qc']
        saved_seconds = lambda values: sum(portions(r, include_saved=True).get(day, 0) for r in values)
        return dict(day=day, timezone='Asia/Shanghai', total_seconds=sum(portions(r).get(day, 0) for r in qualified),
                    saved_seconds=saved_seconds(saved), saved_count=len(saved),
                    pending_qc_seconds=saved_seconds(pending), pending_qc_count=len(pending),
                    saved_unqualified_seconds=saved_seconds(other), saved_unqualified_count=len(other),
                    qualified_count=len(qualified), pass_count=sum(r.get('qc_level') == 'INFO' for r in qualified),
                    warn_count=sum(r.get('qc_level') == 'WARN' for r in qualified),
                    updated_at=updated, error=error, ready=updated is not None, rows=sorted(rows, key=lambda r:(r['start'], r['id'])))

    def text(self, day='today'):
        data = self.snapshot(day)
        rows, day = data['rows'], data['day']
        measured = [r for r in rows if r.get('has_data') and r.get('qc_done')]
        clean = [r for r in measured if r.get('qc_level') in ('INFO', 'WARN')]
        lines = ['会话汇总 — ' + day, '=' * 68,
                 '  设备：' + self.store.roots()[1].name + ' · 统计时区：北京时间',
                 '  录制/准备尝试 %d 条' % len(rows),
                 '  其中空录制 %d 条（∅，不计入无误比例）' % sum(not r.get('has_data') for r in rows),
                 '  有数据但缺 QC 报告 %d 条（?，不计入无误比例）' % sum(r.get('has_data') and not r.get('qc_done') for r in rows),
                 ('  无误数据：%d/%d (%.1f%%) · 分母 = 有数据且跑过 QC 的录制' % (len(clean), len(measured), 100 * len(clean) / len(measured))) if measured else '  无误数据：0/0（暂无可计算比例的数据）',
                 '  合格可推送录制：%d 条（通过 %d · WARN %d）' % (data['qualified_count'], data['pass_count'], data['warn_count']),
                 '  已保存采集时长：%.1f 分钟（%.1f 秒） · %d 段' % (data['saved_seconds'] / 60, data['saved_seconds'], data['saved_count']),
                 '  其中待主机质检：%.1f 秒 · %d 段；已保存未合格/未确认质检：%.1f 秒 · %d 段' % (data['pending_qc_seconds'], data['pending_qc_count'], data['saved_unqualified_seconds'], data['saved_unqualified_count']),
                 '  有效录制时长合计：%.1f 分钟（%.1f 秒）' % (data['total_seconds'] / 60, data['total_seconds']),
                 '  已保存时长：确认保留且有数据，包括待主机质检；不含未保存、放弃或删除；跨午夜按实际秒数分日。',
                 '  合格时长：确认保留且通过/WARN；待主机质检、ERROR不计入合格。',
                 '  已上传并清理本地副本的数据仍计入；✓表示QC无ERROR，不等于已确认保存。', '']
        for severity, heading, limit in [('ERROR', '错误（按检查项 → 流）', None), ('WARN', '警告（按检查项 → 流，前 10）', 10)]:
            groups = defaultdict(lambda:dict(count=0, sessions=set(), streams=Counter()))
            for row in rows:
                for finding in row.get('findings', []):
                    if finding['level'] == severity:
                        group = groups[finding['check']]
                        group['count'] += 1
                        group['sessions'].add(row['id'])
                        group['streams'][finding['stream']] += 1
            lines.append('  ' + heading + '：')
            if not groups:
                lines.append('    无')
            for check, group in sorted(groups.items(), key=lambda pair:(-pair[1]['count'], pair[0]))[:limit]:
                lines.append('    %-26s %d 条 · %d/%d 个 session (%.1f%%)' % (check, group['count'], len(group['sessions']), len(rows), 100 * len(group['sessions']) / len(rows)))
                lines.append('        流：' + ' · '.join('%s %d' % pair for pair in group['streams'].most_common()))
            lines.append('')
        lines.append('  明细（全部尝试，按时间；结局 = 保留/未成功保留/重采或放弃/删除/待确认）：')
        for row in rows:
            symbol = '∅' if not row.get('has_data') else '?' if not row.get('qc_done') else '✗' if row.get('qc_level') == 'ERROR' else '✓'
            outcome = '重采/放弃' if row.get('status') == 'discarded' else '删除' if row.get('status') == 'deleted' else '保留' if row.get('qualified') else '已保存/待主机质检' if saved_recording(row) and row.get('status') == 'pending_qc' else '未成功保留' if row.get('retained') else '待确认/失败'
            time_label = dt.datetime.fromtimestamp(row['start'], CST).strftime('%Y-%m-%d-%H-%M-%S')
            lines.append('    %s %s  任务[%s]  %.1fs  %s  %s  %d ERROR · %d WARN%s  [%s]' %
                         (symbol, time_label, row['reference'], row['duration'], row.get('qc_level') or '未QC', outcome,
                          row.get('errors', 0), row.get('warnings', 0), ' · 已上传/本地清理' if row.get('local_cleaned') else ' · 已上传' if row.get('uploaded') else '', row['id']))
        coverage = Counter(r['reference'] for r in rows if r.get('has_data'))
        lines += ['', '  任务覆盖：当日有数据的参考任务 %d 个' % len(coverage)]
        lines += ['    录了 %d 次：%s' % (n, name) for name, n in sorted(coverage.items())]
        if data['error']:
            lines += ['', '  统计提示：' + data['error']]
        lines += ['', '  生成时间：' + dt.datetime.fromtimestamp(self.clock(), CST).isoformat(timespec='seconds'), '']
        return '\n'.join(lines)

    def export(self, day='today'):
        if not self.export_lock.acquire(False):
            raise ValueError('每日汇总正在导出，请稍后重试')
        try:
            return self._export(day)
        finally:
            self.export_lock.release()

    def _export(self, day='today'):
        if self.busy():
            raise ValueError('请结束采集准备或录制后再导出每日汇总')
        if not self.refresh(wait=True):
            raise ValueError('每日统计正在更新，请稍后重试')
        if self.busy():
            raise ValueError('请结束采集准备或录制后再导出每日汇总')
        day = self.snapshot(day)['day']
        _, volume = self.store.roots()
        directory = volume / 'delivery/daily-summaries'
        directory.mkdir(parents=True, exist_ok=True)
        raw = self.text(day)
        path = directory / ('collection-summary-' + day + '.txt')
        from cloud_common import sync_dir
        import os
        temporary = path.with_suffix('.txt.tmp')
        with temporary.open('w', encoding='utf-8-sig', newline='\n') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        sync_dir(directory)
        return path, raw

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(3)
