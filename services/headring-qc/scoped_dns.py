"""Process-local DNS fallback for the reviewed upload hosts; TLS is never disabled."""
import http.client
import ipaddress
import json
import socket
import ssl
import threading
import time
from urllib.parse import urlencode

HOSTS=frozenset(('ei2rmd.baai.ac.cn','ks3-cn-beijing.ksyuncs.com',
                'baai-eai-datasets.ks3-cn-beijing.ksyuncs.com'))
_original_getaddrinfo=socket.getaddrinfo
_original_gethostbyname=socket.gethostbyname
_cache={};_lock=threading.RLock()

class _DnsConnection(http.client.HTTPSConnection):
    def __init__(self,hostname,address,**kwargs):
        super().__init__(hostname,**kwargs);self.address=address
    def connect(self):
        # A documented AliDNS address is used only for DNS. Its DNS hostname
        # remains the SNI/certificate verification target, not an unchecked IP.
        sock=socket.create_connection((self.address,443),timeout=self.timeout)
        self.sock=self._context.wrap_socket(sock,server_hostname=self.host)

def resolve(host):
    if host not in HOSTS:raise ValueError('Host outside reviewed DNS fallback scope')
    with _lock:
        cached=_cache.get(host)
        if cached and cached[0]>time.monotonic():return cached[1]
        failures=[]
        for hostname,address,path in [('dns.alidns.com','223.6.6.6','/resolve'),
                                      ('cloudflare-dns.com','1.1.1.1','/dns-query')]:
            connection=_DnsConnection(hostname,address,timeout=4,context=ssl.create_default_context())
            try:
                connection.request('GET',path+'?'+urlencode({'name':host,'type':'A'}),headers={'Accept':'application/dns-json'})
                response=connection.getresponse()
                if response.status!=200:raise OSError('Scoped DNS resolver HTTP '+str(response.status))
                body=json.loads(response.read(65536))
                if body.get('Status')!=0:raise OSError('Scoped DNS lookup failed')
                addresses=[];ttl=300
                for row in body.get('Answer',[]):
                    if row.get('type')!=1:continue
                    ip=ipaddress.ip_address(row['data'])
                    if ip.version!=4 or not ip.is_global:raise OSError('Scoped DNS returned nonpublic address')
                    addresses.append(str(ip));ttl=min(ttl,int(row.get('TTL',60)))
                if not addresses:raise OSError('Scoped DNS returned no public IPv4 address')
                _cache[host]=(time.monotonic()+max(1,ttl),tuple(addresses));return tuple(addresses)
            except (OSError,ValueError,http.client.HTTPException) as error:failures.append(type(error).__name__)
            finally:connection.close()
        raise OSError('Both scoped HTTPS DNS resolvers unavailable: '+','.join(failures))

def install():
    def getaddrinfo(host,port,*args,**kwargs):
        name=host.decode('ascii') if isinstance(host,bytes) else host
        if name not in HOSTS:return _original_getaddrinfo(host,port,*args,**kwargs)
        results=[]
        for address in resolve(name):results.extend(_original_getaddrinfo(address,port,*args,**kwargs))
        return results
    def gethostbyname(host):
        return resolve(host)[0] if host in HOSTS else _original_gethostbyname(host)
    socket.getaddrinfo=getaddrinfo;socket.gethostbyname=gethostbyname
