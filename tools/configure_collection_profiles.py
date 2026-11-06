#!/usr/bin/env python3
"""Assign the existing files to their original mode and create an empty other mode."""
import argparse,json,pathlib,sys

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root',type=pathlib.Path,default=pathlib.Path(__file__).resolve().parents[1])
    parser.add_argument('--legacy-mode',choices=['emg','eeg'],required=True)
    args=parser.parse_args();base=args.project_root.resolve()/'orangepi'
    state=base/'runtime/controller.json'
    if state.exists() and json.loads(state.read_text()).get('state') not in ('idle','device_confirm','failed_wait','exhausted','stopped'):
        parser.error('End the current recording and stop the collection service first.')
    sys.path.insert(0,str(base/'src/orangepi_collect/common'))
    from collection_profiles import initialize
    value=initialize(base,args.legacy_mode)
    print(json.dumps(value,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
