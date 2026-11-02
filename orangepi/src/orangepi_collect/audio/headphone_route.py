"""Optional, explicit Pulse playback route. Never changes a sink/source default.

No file or {"enabled": false} preserves the original paplay command exactly.
Enabled routes require a named sink and a local Pulse server; the native player
checks its stream target and completes its drain before returning success.
"""
from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess

GUARDED_PLAYER = '/opt/opi-headphone-audio/guarded-paplay'
CONFIG_BYTES = 4096


class AudioRouteError(ValueError):
    def __init__(self, message, strict_requested=False):
        super().__init__(message)
        self.strict_requested = strict_requested


def _unique_object(pairs):
    value = {}
    for name, item in pairs:
        if name in value:
            strict = any(key == 'enabled' and val is True for key, val in pairs)
            raise AudioRouteError('耳机输出配置包含重复字段', strict)
        value[name] = item
    return value


def _invalid_constant(value):
    raise AudioRouteError('耳机输出配置包含无效数值')


def _field(value, message):
    if (not isinstance(value, str) or not value or len(value) > 512
            or value.strip() != value or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise AudioRouteError(message, True)
    return value


@dataclass(frozen=True)
class OutputRoute:
    enabled: bool = False
    configured: bool = False
    server: str = ''
    sink: str = ''
    player: str = GUARDED_PLAYER

    @classmethod
    def load(cls, base):
        path = Path(base) / 'configs' / 'audio_output.json'
        try:
            with path.open('rb') as handle:
                raw = handle.read(CONFIG_BYTES + 1)
        except FileNotFoundError:
            return cls()
        except OSError as exc:
            raise AudioRouteError('无法读取耳机输出配置，禁止回退默认输出') from exc
        if len(raw) > CONFIG_BYTES:
            raise AudioRouteError('耳机输出配置过大，禁止回退默认输出')
        try:
            value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                               parse_constant=_invalid_constant)
        except AudioRouteError:
            raise
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise AudioRouteError('耳机输出配置损坏，禁止回退默认输出') from exc
        if not isinstance(value, dict) or type(value.get('enabled')) is not bool:
            raise AudioRouteError('耳机输出配置须明确指定 enabled 布尔值')
        if value['enabled'] is False:
            return cls(configured=True)
        if set(value) - {'enabled', 'server', 'sink', 'player'}:
            raise AudioRouteError('耳机输出配置包含未知字段', True)
        server = _field(value.get('server'), '严格耳机输出须指定 Pulse 服务器')
        if not re.fullmatch(r'(?:unix:)?/[A-Za-z0-9_./-]+', server):
            raise AudioRouteError('耳机输出须使用明确的本机 Pulse socket', True)
        sink = _field(value.get('sink'), '严格耳机输出须指定明确的输出设备名')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}', sink) or sink.isdigit():
            raise AudioRouteError('耳机输出须使用稳定的明确设备名，不能使用默认别名或索引', True)
        player = value.get('player', GUARDED_PLAYER)
        if player != GUARDED_PLAYER:
            raise AudioRouteError('严格耳机播放器路径不受支持', True)
        return cls(True, True, server, sink, player)

    def environment(self, original):
        if not self.enabled:
            return original
        env = dict(original)
        env['PULSE_SERVER'] = self.server
        env['PULSE_SINK'] = self.sink
        return env

    def validate_sink(self, original):
        if not self.enabled:
            return
        try:
            result = subprocess.run(['pactl', '--server', self.server, 'list', 'short', 'sinks'],
                                    env=self.environment(original), capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AudioRouteError('无法验证耳机输出，禁止回退默认输出', True) from exc
        if result.returncode or not isinstance(result.stdout, str) or len(result.stdout) > 65536:
            raise AudioRouteError('耳机音频服务不可用，禁止回退默认输出', True)
        # pactl's short format is index, exact symbolic name, driver, format,
        # state. No substring matching and no aliases such as @DEFAULT_SINK@.
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[1] == self.sink:
                return
        raise AudioRouteError('指定耳机输出不存在，禁止回退默认输出', True)

    def playback(self, path, original):
        if not self.enabled:
            return ['paplay', str(path)], original
        # The child validates READY's exact device and guards it through drain.
        # Do not run a blocking pactl probe here: cancellation/skipping must be
        # able to terminate connection and sink validation as well as playback.
        return [self.player, '--server', self.server, '--sink', self.sink, str(path)], self.environment(original)

    def volume(self, percent, original):
        if not self.enabled:
            return ['pactl', 'set-sink-volume', '@DEFAULT_SINK@', str(percent) + '%'], original
        self.validate_sink(original)
        return ['pactl', '--server', self.server, 'set-sink-volume', self.sink, str(percent) + '%'], self.environment(original)
