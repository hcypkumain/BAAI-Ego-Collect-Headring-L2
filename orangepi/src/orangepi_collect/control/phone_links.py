"""Local address presentation only; never switch transport or transfer login."""
import ipaddress,json,subprocess,threading,time
from pathlib import Path
from phone_transport import device_id, load_config, read_cache

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

class PhoneLinks:
    def __init__(self,owner,provider=None,clock=time.monotonic):
        self.owner=owner;self.provider=provider or local_links;self.clock=clock;self.lock=threading.Lock();self.cached=[];self.cached_at=-1e9
    def links(self):
        with self.lock:
            now=self.clock()
            if now-self.cached_at>5:
                self.cached=[v for v in self.provider(self.owner.server.server_port) if v.get('kind')=='wireless'];self.cached_at=now
            return [dict(value) for value in self.cached]
    def snapshot(self,local_address=None):
        privacy=getattr(self.owner,'privacy',None)
        identity=privacy.snapshot().get('instance') if privacy else None
        try:config=load_config()
        except (OSError,ValueError):
            return dict(mode='error',current='unknown',configured_transport=None,current_transport='unknown',state='error',reason='invalid_configuration',links=[],
                        device_id=device_id(),boot_id=getattr(self.owner,'boot_id',None),controller_identity=identity)
        mode=config['mode'];result=read_cache(config=config) if mode=='usb_tether' else dict(state='absent',reason='adb_not_observed',links=[])
        links=result['links'] if mode=='usb_tether' else self.links()
        links=[dict(v,iface=v['interface'],origin='http://'+v['address']+':'+str(self.owner.server.server_port)) for v in links]
        try:address=ipaddress.ip_address(local_address[0] if isinstance(local_address,tuple) else local_address)
        except (ValueError,TypeError):address=None
        current='unknown'
        if address is not None:
            if address.is_loopback:current='loopback'
            else:current=next((v['kind'] for v in links if v['address']==str(address)),'unknown')
        return dict(mode='wifi' if mode=='adb' else mode,current=current,configured_transport=mode,
                    current_transport=current,state=result['state'],reason=result['reason'],links=links,
                    device_id=device_id(),boot_id=getattr(self.owner,'boot_id',None),controller_identity=identity)
