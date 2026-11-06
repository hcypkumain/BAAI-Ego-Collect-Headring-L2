"""Single source of paths and device configuration for the Pi addon."""
from pathlib import Path
import sys
import yaml
BASE=Path(__file__).resolve().parents[3]
PROJECT=BASE.parent
SRC=BASE/'src/orangepi_collect'
sys.path.insert(0,str(BASE/'src'))
for directory in SRC.iterdir():
    if directory.is_dir():sys.path.insert(0,str(directory))
sys.path.insert(0,str(PROJECT/'src'))
def devices():
    import json, os
    snapshot = os.environ.get('ORANGEPI_DEVICE_SELECTION')
    if snapshot:
        selection = json.loads(Path(snapshot).read_text(encoding='utf-8'))
        if 'device_config' in selection:
            config = selection['device_config']
            config.pop('eeg', None)
            return config
    from device_discovery import discover
    from wrist_calibration import apply
    from emg_calibration import apply as apply_emg
    from wrist_camera_settings import apply as apply_wrist_settings
    config = yaml.safe_load((BASE/'configs/devices.yaml').read_text(encoding='utf-8'))
    config.pop('eeg', None)
    config = apply(discover(config, BASE), BASE)
    return apply_wrist_settings(apply_emg(config, BASE), BASE)
