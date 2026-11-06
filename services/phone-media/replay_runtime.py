"""Lazy, storage-checked replay integration without acquisition controls."""
import json
from pathlib import Path
import subprocess
import threading
import time
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class ReplayUnavailable(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


class ReplayRuntime:
    def __init__(self, base, token=''):
        self.code_base = Path(base).resolve()
        self.base = self.code_base
        self.profile_context = None
        self.allow_missing_root = False
        self.token = token
        self.lock = threading.RLock()
        self.instance = None
        self.root = None
        self.storage_until = self.controller_until = 0.
        self.controller_active = True
        self.last_error = None
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())
        self.closed = False
        self.monitor_stop = threading.Event()
        self.monitor = None

    def _storage(self):
        selector=self.code_base/'runtime/collection_profile.json'
        if selector.exists():
            value=json.loads(selector.read_text(encoding='utf-8'))
            if value.get('active_mode') not in ('emg','eeg') or value.get('legacy_mode') not in ('emg','eeg'):raise ReplayUnavailable('采集模式无效')
            key=(value['active_mode'],value['generation'])
            if key!=self.profile_context:
                if self.instance is not None:self.instance.release()
                self.instance=None;self.root=None;self.storage_until=self.controller_until=0.
                self.base=self.code_base if value['active_mode']==value['legacy_mode'] else self.code_base.parent/'profiles'/value['active_mode']/'orangepi'
                self.profile_context=key
        if time.monotonic() < self.storage_until and self.root is not None:
            return self.root
        import yaml
        config = yaml.safe_load((self.base/'configs/storage.yaml').read_text())
        self.allow_missing_root = config.get('capture_profile') in ('emg','eeg')
        if config.get('mode') != 'sd':
            raise ReplayUnavailable('回看需要已配置的采集SD卡')
        mount = Path(config['mount_point']).resolve()
        for field in ('directory', 'device_id'):
            value = config.get(field)
            if not isinstance(value, str) or not value or Path(value).name != value or value in ('.', '..'):
                raise ReplayUnavailable('采集存储配置无效')
        result = subprocess.run(['findmnt', '-J', '-M', str(mount), '-o', 'TARGET,UUID'],
                                capture_output=True, text=True, timeout=3)
        try:
            volume = json.loads(result.stdout)['filesystems'][0]
            if (result.returncode or volume['target'] != str(mount) or
                    str(volume.get('uuid', '')).upper() != str(config['expected_uuid']).upper()):
                raise ValueError()
        except (KeyError, IndexError, TypeError, ValueError):
            raise ReplayUnavailable('采集SD卡未挂载或身份不符') from None
        volume=mount/config['directory']/config['device_id']
        if config.get('capture_profile'):
            if config['capture_profile'] not in ('emg','eeg'):raise ReplayUnavailable('存储模式无效')
            volume=volume/'profiles'/config['capture_profile']
        root = (volume/'data').resolve()
        try:
            root.relative_to(mount)
        except ValueError:
            raise ReplayUnavailable('采集目录超出已配置存储') from None
        if self.root is not None and root != self.root:
            raise ReplayUnavailable('采集存储配置已改变，请重新加载回看服务')
        self.root = root
        self.storage_until = time.monotonic()+1.
        return root

    def active_check(self):
        # No sensor is opened and no acquisition command is sent. Keep heavy
        # playback away from capture preparation/recording/decoder-based QC.
        with self.lock:
            self._storage()
            if time.monotonic() < self.controller_until:
                return self.controller_active
            request = Request('http://127.0.0.1:8765/api/display-state',
                              headers={'X-Access-Token': self.token} if self.token else {})
            try:
                with self.opener.open(request, timeout=1.5) as response:
                    raw = response.read(512*1024+1)
                if len(raw) > 512*1024:
                    raise ValueError('oversized controller status')
                status = json.loads(raw)
                worker = status.get('worker') or {}
                state = status.get('state')
                active = state not in ('idle', 'device_confirm', 'failed_wait', 'exhausted', 'waiting_enter', 'stopped')
                if state == 'confirm_stop' and worker.get('state') in ('qc_ok', 'qc_warn', 'qc_error', 'done', 'completed', 'failed','success','cancelled','error'):
                    active = False
                if status.get('busy') or worker.get('recording_active'):
                    active = True
                if status.get('privacy',{}).get('required') or status.get('privacy',{}).get('stop_confirmed') is not True:active=True
            except Exception:
                self.controller_active = True
                self.controller_until = 0.
                raise ReplayUnavailable('无法确认采集空闲，回看暂时暂停') from None
            self.controller_active = bool(active)
            self.controller_until = time.monotonic()+.25
            return self.controller_active

    def backend(self):
        with self.lock:
            if self.closed:
                raise ReplayUnavailable('回看服务已关闭')
            try:
                root = self._storage()
                if self.instance is None:
                    from recording_replay import RecordingReplay
                    from replay_video import VideoDecoder
                    self.instance = RecordingReplay(root, video_decoder=VideoDecoder(), active_check=self.active_check,allow_missing_root=self.allow_missing_root)
                    if self.monitor is None:
                        self.monitor = threading.Thread(target=self._watch_capture, daemon=True)
                        self.monitor.start()
                self.last_error = None
                return self.instance
            except Exception as exc:
                self.last_error = str(exc)
                raise

    def _watch_capture(self):
        while not self.monitor_stop.wait(.5):
            with self.lock:
                instance = self.instance
            if instance is None:
                continue
            decoder = getattr(instance, 'video_decoder', None)
            if decoder is None:
                continue
            if not decoder.is_active():continue
            try:
                busy = self.active_check()
            except Exception:
                busy = True
            if busy:
                instance.release()

    def health(self):
        with self.lock:
            result = dict(enabled=True, initialized=self.instance is not None,
                          complete_frame_priority=True, error=self.last_error)
            if self.instance is not None and hasattr(self.instance, 'health'):
                result['backend'] = self.instance.health()
            return result

    def close(self):
        self.monitor_stop.set()
        with self.lock:
            self.closed = True
            instance, self.instance = self.instance, None
        if instance is not None:
            instance.close()
        if self.monitor is not None:
            self.monitor.join(timeout=2)
