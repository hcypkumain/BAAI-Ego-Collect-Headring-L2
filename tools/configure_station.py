#!/usr/bin/env python3
"""Create missing local preferences; never replace an existing station state."""
import argparse,json,os,sys
from pathlib import Path

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--collector-id',required=True,help='This physical collection station ID, e.g. L2-001')
    args=parser.parse_args();root=args.project_root.resolve();base=root/'orangepi'
    sys.path.insert(0,str(base/'src/orangepi_collect/common'))
    from session_naming import collector_id
    identity=collector_id(args.collector_id)
    for folder in (base/'runtime',base/'private'):folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    preferences={
        'reference_control.json':dict(version=1,collector_id=identity,selected_list_id='all'),
        'headphone_selection.json':dict(version=1,address=None),
        'audio_output.json':dict(enabled=False,server='unix:/tmp/pulse-socket',sink='',player='/opt/opi-headphone-audio/guarded-paplay')}
    for name,value in preferences.items():
        path=base/'configs'/name
        try:
            fd=os.open(str(path),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        except FileExistsError:
            print('Preserved existing '+str(path));continue
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            json.dump(value,stream,ensure_ascii=False);stream.write('\n');stream.flush();os.fsync(stream.fileno())
        print('Created '+str(path))
    print('Review devices.yaml and storage.yaml for this station before starting services.')

if __name__=='__main__':main()
