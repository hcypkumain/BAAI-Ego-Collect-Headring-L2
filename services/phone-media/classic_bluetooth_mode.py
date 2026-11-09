"""Read-only evidence for classic Bluetooth availability; never changes radio state."""
import configparser
import ctypes
import re
import socket
import struct
import time
from pathlib import Path


def disk_mode(path=Path('/etc/bluetooth/main.conf')):
    parser=configparser.ConfigParser(interpolation=None,strict=True)
    try:
        with path.open(encoding='utf-8') as stream:parser.read_file(stream)
    except FileNotFoundError:return 'default_dual'
    except (OSError,UnicodeError,configparser.Error):return 'unknown'
    value=parser.get('General','ControllerMode',fallback='dual').strip().lower()
    return value if value in ('le','dual','bredr') else 'unknown'


def runtime_mode(adapter):
    """Read MGMT READ_INFO when permitted; inaccessible is unknown, never disabled."""
    match=re.fullmatch(r'/org/bluez/hci([0-9]+)',str(adapter or ''))
    if not match:return None
    index=int(match.group(1))
    if index>65534:return None
    s=None
    try:
        s=socket.socket(31,3,1); addr=ctypes.create_string_buffer(struct.pack('HHH',31,65535,3))
        libc=ctypes.CDLL(None,use_errno=True)
        if libc.bind(s.fileno(),addr,6):return None
        s.settimeout(.5);s.sendall(struct.pack('<HHH',4,index,0));deadline=time.monotonic()+.8
        while time.monotonic()<deadline:
            data=s.recv(1024)
            if len(data)<9:continue
            event,idx,n,opcode,status=struct.unpack_from('<HHHHB',data)
            if idx!=index or event not in (1,2) or opcode!=4:continue
            if len(data)!=6+n or status or event!=1 or len(data)<26:return None
            supported,current=struct.unpack_from('<II',data,18)
            return {'supported':bool(supported&0x80),'enabled':bool(current&0x80)}
    except (OSError,ValueError,OverflowError,struct.error):return None
    finally:
        if s is not None:s.close()


def assess(mode, runtime):
    """Return (blocking error, warning); disk and runtime are separate evidence."""
    if runtime is not None:
        if not runtime['supported']:return 'classic_unsupported',''
        if not runtime['enabled']:
            return ('classic_not_loaded' if mode in ('dual','default_dual') else 'classic_disabled'),''
        if mode=='le':return '', 'classic_config_le_enabled'
        return '', ''
    if mode=='le':return '', 'classic_config_le_unknown'
    return '', 'classic_runtime_unknown'
