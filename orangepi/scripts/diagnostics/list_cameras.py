#!/usr/bin/env python3
"""List real UVC image nodes, stable port paths, USB identity and link speed."""
import argparse
import json
from pathlib import Path
import subprocess


def discover():
    result=[]
    for link in sorted(Path('/dev/v4l/by-path').glob('*usb*video-index0')):
        node=link.resolve()
        sysnode=Path('/sys/class/video4linux')/node.name
        usb=next((p for p in (sysnode/'device').resolve().parents if (p/'idVendor').exists()),None)
        if usb is None:continue
        def read(name):
            p=usb/name
            return p.read_text().strip() if p.exists() else None
        result.append(dict(device=str(link),current_node=str(node),
            name=(sysnode/'name').read_text().strip(),usb_port=usb.name,
            usb_id=read('idVendor')+':'+read('idProduct'),serial=read('serial'),
            speed_mbps=read('speed')))
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--formats',action='store_true')
    args=parser.parse_args()
    cameras=discover()
    print(json.dumps(cameras,ensure_ascii=False,indent=2))
    if args.formats:
        for camera in cameras:
            print('\n'+camera['device'],flush=True)
            subprocess.run(['v4l2-ctl','-d',camera['device'],'--list-formats-ext'],check=True)
