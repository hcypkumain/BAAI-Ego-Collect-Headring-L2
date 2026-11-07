#!/usr/bin/env python3
"""Migrate installed units to the early boot page without restarting capture."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time


def rewrite(text, clock_unit=False):
    lines=[]
    for line in text.splitlines():
        if not clock_unit and line.startswith('ExecStartPre=') and '/run/camd.sock' in line:
            continue
        key,separator,value=line.partition('=')
        if key.strip() in (['Before'] if clock_unit else ['After','Requisite']):
            remove=['embodied-pi-collect.service'] if clock_unit else ['camd.service','headring-lan-time.service']
            if key.strip()=='After' and not clock_unit:
                remove+=['network-online.target','network.target','bluetooth.service','display-manager.service','lightdm.service','sound.target','seeker_imu.service','seeker_cam_bridge.service']
            values=[item for item in value.split() if item not in remove]
            if not values:
                continue
            line=key.strip()+'='+' '.join(values)
        lines.append(line)
    return '\n'.join(lines)+'\n'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run',action='store_true')
    args=parser.parse_args()
    base=Path(__file__).resolve().parents[1]
    control=base/'src/orangepi_collect/control'
    for name in ['service_entry.py','startup_status.py']:
        if not (control/name).is_file():raise SystemExit('Install the early-web program files first')
    if 'ORANGEPI_EARLY_WEB' not in (base/'scripts/run.sh').read_text():
        raise SystemExit('Update run.sh before changing unit ordering')
    unit=Path('/etc/systemd/system/embodied-pi-collect.service')
    clock=Path('/etc/systemd/system/headring-lan-time.service')
    paths=[unit]+sorted(Path(str(unit)+'.d').glob('*.conf'))+[clock]
    changes=[(path,path.read_bytes(),rewrite(path.read_text(),clock_unit=path==clock).encode())
             for path in paths if path.is_file()]
    changes=[item for item in changes if item[1]!=item[2]]
    if args.dry_run:
        print(json.dumps({'changed_files':[str(p)for p,_,_ in changes],'restart':False},indent=2));return
    if os.geteuid()!=0:raise SystemExit('Run this migration with sudo')
    if not changes:print('Early-web unit ordering is already installed');return
    backup=Path('/home/orangepi/maintenance')/('early-web-units-'+str(time.time_ns()))
    backup.mkdir(parents=True)
    records=[]
    for index,(path,old,new) in enumerate(changes):
        stat=path.stat();saved=backup/str(index);saved.write_bytes(old)
        records.append({'path':str(path),'backup':str(saved),'before':hashlib.sha256(old).hexdigest(),'mode':stat.st_mode&0o777,'uid':stat.st_uid,'gid':stat.st_gid})
    (backup/'manifest.json').write_text(json.dumps(records,indent=2))
    def replace(path,data,record):
        temp=path.with_name(path.name+'.early-web-new');temp.write_bytes(data)
        os.chmod(temp,record['mode']);os.chown(temp,record['uid'],record['gid']);os.replace(temp,path)
    try:
        for (path,old,new),record in zip(changes,records):
            if path.read_bytes()!=old:raise RuntimeError('Unit changed during migration: '+str(path))
            replace(path,new,record)
        subprocess.run(['systemctl','daemon-reload'],check=True)
    except BaseException:
        for (path,old,new),record in zip(changes,records):replace(path,old,record)
        subprocess.run(['systemctl','daemon-reload'],check=False)
        raise
    print(json.dumps({'backup':str(backup),'changed_files':[str(p)for p,_,_ in changes],'restart':False},indent=2))


if __name__=='__main__':main()
