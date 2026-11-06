"""Single source of paths and device configuration for the Pi addon."""
from pathlib import Path
import sys
import yaml
CODE_BASE=Path(__file__).resolve().parents[3]
CODE_PROJECT=CODE_BASE.parent
from collection_profiles import selection,profile_base,context
PROFILE=selection(CODE_BASE)
COLLECTION_MODE=PROFILE['active_mode']
COLLECTION_CONTEXT=context(PROFILE)
BASE=profile_base(CODE_BASE,PROFILE)
PROJECT=BASE.parent
SRC=CODE_BASE/'src/orangepi_collect'
sys.path.insert(0,str(BASE/'src'))
for directory in SRC.iterdir():
    if directory.is_dir():sys.path.insert(0,str(directory))
sys.path.insert(0,str(CODE_PROJECT/'src'))
def devices():
    import json, os
    snapshot = os.environ.get('ORANGEPI_DEVICE_SELECTION')
    if snapshot:
        selection = json.loads(Path(snapshot).read_text(encoding='utf-8'))
        if 'device_config' in selection:
            return selection['device_config']
    from eeg_settings import apply as apply_eeg
    from device_discovery import discover
    from wrist_calibration import apply
    from emg_calibration import apply as apply_emg
    from wrist_camera_settings import apply as apply_wrist_settings
    from collection_profiles import device_config
    config = device_config(yaml.safe_load((BASE/'configs/devices.yaml').read_text(encoding='utf-8')), COLLECTION_MODE)
    if COLLECTION_MODE == 'eeg':config = apply_eeg(config, BASE)
    config = apply(discover(config, BASE), BASE)
    return apply_wrist_settings(apply_emg(config, BASE), BASE)
