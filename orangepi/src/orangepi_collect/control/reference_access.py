"""On-device upload address discovery; never changes Wi-Fi or the wired route."""
import copy
import ipaddress
import json
from pathlib import Path
import subprocess
import threading
import time
from urllib.parse import quote, urlsplit

from reference_credentials import TRUSTED_PEERS


def network_addresses(records, wireless=None):
    """Use actual global interface addresses, preferring Wi-Fi over local LAN."""
    wireless = wireless or (lambda name: (Path('/sys/class/net') / name / 'wireless').is_dir())
    result = []
    seen = set()
    if not isinstance(records, list):
        return result
    for item in records[:128]:
        if not isinstance(item, dict):
            continue
        name = item.get('ifname', '')
        if (not isinstance(name, str) or not name or len(name) > 64
                or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-' for c in name)
                or name.startswith(('lo', 'docker', 'veth', 'virbr', 'br-', 'tun', 'tap', 'wg', 'easytier', 'utun'))
                or item.get('operstate') not in ('UP', 'UNKNOWN')):
            continue
        entries = item.get('addr_info', [])
        if not isinstance(entries, list):
            continue
        for entry in entries[:32]:
            if not isinstance(entry, dict) or entry.get('family') != 'inet' or entry.get('scope') != 'global':
                continue
            try:
                address = ipaddress.ip_address(entry.get('local', ''))
            except (ValueError, TypeError):
                continue
            if (address.version != 4 or address.is_loopback or address.is_link_local
                    or address.is_multicast or address.is_unspecified
                    or str(address) in ('192.168.250.1', '192.168.250.2')
                    or address in ipaddress.ip_network('10.231.47.0/24')
                    or str(address) in seen):
                continue
            seen.add(str(address))
            result.append(dict(interface=name, address=str(address), wireless=bool(wireless(name))))
    result.sort(key=lambda row: (not row['wireless'], row['interface'], row['address']))
    return result[:8]


class ReferenceAccess:
    def __init__(self, credentials, collector_provider=None, runner=None, clock=time.monotonic):
        self.credentials = credentials
        self.collector_provider = collector_provider or (lambda: '')
        self.runner = runner or subprocess.run
        self.clock = clock
        self.lock = threading.Lock()
        self.cached = None
        self.expires = 0

    def _networks(self):
        try:
            result = self.runner(['ip', '-j', '-4', 'address', 'show'],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=2, check=False)
            if result.returncode or len(result.stdout) > 256 * 1024:
                return []
            return network_addresses(json.loads(result.stdout))
        except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
            return []

    def snapshot(self):
        # Called only by the privileged HTTP access-info route, never by the
        # controller status/poll thread. No token is logged or sent as a query.
        with self.lock:
            now = self.clock()
            if self.cached is None or now >= self.expires:
                self.cached = self._networks()
                self.expires = now + 3
            networks = copy.deepcopy(self.cached)
        code = self.credentials.code()
        urls = ['http://' + row['address'] + ':8765/references#code=' + quote(code, safe='')
                for row in networks]
        try:
            collector = self.collector_provider()
        except Exception:
            collector = ''
        if not isinstance(collector, str) or len(collector) > 48:
            collector = ''
        return dict(version=1, management_url=urls[0] if urls else '',
            access_code=code, local_urls=urls, collector_id=collector,
            network_name=('Pi 无线网络' if networks[0]['wireless'] else 'Pi 局域网') if networks else '',
            help='手机或电脑与 Pi 连接同一个 Wi-Fi；也可让 Pi 连接手机热点。'
                 '扫描二维码打开图库，上传、编排或删除参考图。'
                 if urls else 'Pi 当前没有可供手机访问的网络地址。请先让 Pi 连接同一 Wi-Fi 或手机热点，再打开此页。')


def handle_reference_access(handler, owner):
    """Only physical/control-plane peers may retrieve the library capability."""
    try:
        url = urlsplit(handler.path)
    except ValueError:
        return False
    if url.path != '/api/reference-access':
        return False
    if handler.path != '/api/reference-access':
        handler.send(400, {'error': 'invalid_path'})
        return True
    seen = set()
    for key, value in handler.headers.items():
        key = key.lower()
        if key in seen or len(value) > 1024:
            handler.send(400, {'error': 'invalid_headers'})
            return True
        seen.add(key)
    if ('transfer-encoding' in seen or handler.headers.get('Content-Length', '0') != '0'):
        handler.send(400, {'error': 'invalid_framing'})
        return True
    if handler.client_address[0] not in TRUSTED_PEERS or 'origin' in seen:
        handler.send(403, {'error': 'on_device_only', 'message': '请在 RV06 的手机上传入口查看管理码'})
        return True
    if not handler.authorized():
        handler.send(401, {'error': 'control_authorization_required'})
        return True
    access = getattr(owner, 'reference_access', None)
    if access is None:
        handler.send(503, {'error': 'library_unavailable', 'message': '图库暂未就绪，请稍后重试'})
        return True
    try:
        handler.send(200, access.snapshot())
    except (OSError, ValueError):
        handler.send(503, {'error': 'library_unavailable', 'message': '图库地址暂不可用，请稍后重试'})
    return True
