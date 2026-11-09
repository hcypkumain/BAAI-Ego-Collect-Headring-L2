#!/usr/bin/env python3
"""Restore this installation's backup only while the collector is idle."""
import json,os,pathlib
import install

if __name__=='__main__':
    if os.geteuid()!=0:raise SystemExit('Administrator authentication required')
    install.verify()
    if not install.idle(install.snapshot()):raise SystemExit('Rollback waits until empty idle workflow')
    result=json.loads((install.ROOT/'install-result.json').read_text())
    backup=pathlib.Path(result['backup']).resolve()
    if backup.parent!=(install.ROOT/'backups').resolve():raise SystemExit('Invalid backup directory')
    tx=install.Transaction.__new__(install.Transaction)
    tx.backup=backup;tx.entries=json.loads((backup/'index.json').read_text());tx.units=json.loads((backup/'units.json').read_text())
    with install.capture_locks():
        if not install.idle(install.snapshot()):raise SystemExit('Workflow became busy')
        for name in install.UNITS+[install.TIMER]:install.run(['systemctl','stop',name],False)
    tx.rollback()
    print(json.dumps(dict(rolled_back=True,backup=str(backup))))
