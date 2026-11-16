"""List Pi Wi-Fi addresses only. No transport switching or login transfer."""
import ipaddress,json,subprocess,threading,time
from pathlib import Path

def local_links(port):
    try:rows=json.loads(subprocess.check_output(['ip','-j','address','show'],timeout=1))
    except (OSError,ValueError,subprocess.SubprocessError):return []
    found=[]
    for row in rows:
        name=row.get('ifname','')
        if 'LOWER_UP' not in row.get('flags',[]) or not ((Path('/sys/class/net')/name/'wireless').exists() or name.startswith('wl')):continue
        for item in row.get('addr_info',[]):
            if item.get('family')!='inet':continue
            try:address=ipaddress.IPv4Address(item['local'])
            except (ValueError,KeyError):continue
            if not address.is_private or address.is_loopback or address.is_link_local or address.is_multicast:continue
            found.append(dict(kind='wireless',interface=name,address=str(address),origin='http://'+str(address)+':'+str(port)))
    return sorted(found,key=lambda value:(value['interface'],value['address']))

def interface_addresses():
    """Read active interface addresses; do not alter existing Wi-Fi link policy."""
    try: rows=json.loads(subprocess.check_output(['ip','-j','address','show'],timeout=2))
    except (OSError,ValueError,subprocess.SubprocessError): return None
    result=[]
    for row in rows:
        name=row.get('ifname','')
        if not isinstance(name,str) or not name or name=='lo' or 'UP' not in row.get('flags',[]): continue
        wireless=(Path('/sys/class/net')/name/'wireless').exists() or name.startswith('wl')
        kind='wireless' if wireless else 'usb' if name.startswith(('usb','rndis','ncm')) else 'other'
        for item in row.get('addr_info',[]):
            try: address=ipaddress.ip_address(item.get('local',''))
            except (TypeError,ValueError): continue
            if address.is_loopback or address.is_unspecified or address.is_multicast: continue
            result.append(dict(interface=name,kind=kind,address=str(address),family='IPv%d'%address.version,scope=item.get('scope','')))
    return sorted(result,key=lambda v:(v['kind'],v['interface'],v['family'],v['address']))

class PhoneLinks:
    def __init__(self,owner,provider=None,clock=time.monotonic):
        self.owner=owner;self.provider=provider or local_links;self.clock=clock;self.lock=threading.Lock();self.cached=[];self.cached_at=-1e9
    def links(self):
        with self.lock:
            now=self.clock()
            if now-self.cached_at>5:
                self.cached=[v for v in self.provider(self.owner.server.server_port) if v.get('kind')=='wireless'];self.cached_at=now
            return [dict(value) for value in self.cached]
    def snapshot(self,origin):return dict(mode='wifi',current='wireless',links=self.links(),interfaces=interface_addresses())
