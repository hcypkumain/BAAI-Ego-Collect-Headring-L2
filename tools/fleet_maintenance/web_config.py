import json, os
from pathlib import Path
HOME=Path.home()
CONFIG=HOME/'.config/headring-fleet-maintenance'
STATE=HOME/'.local/state/headring-fleet-maintenance'
DEFAULT={'ssh_config':str(HOME/'headring-qc/ssh/config'),'bind_interface':'wlx40a5ef5ff812','bind_address':'192.168.20.100','devices':{'cp_%04d'%n:'192.168.20.%d'%ip for n,ip in [(11,104),(13,108),(14,111),(15,109),(17,101),(18,110),(19,105),(20,106),(21,102),(22,107)]}}
def setup():
    for p in (CONFIG,STATE): p.mkdir(mode=0o700,parents=True,exist_ok=True); os.chmod(p,0o700)
    path=CONFIG/'config.json'
    if not path.exists():
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as f: json.dump(DEFAULT,f,indent=2)
    os.chmod(path,0o600)
    return json.loads(path.read_text())
