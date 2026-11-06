"""OmniBCI defaults and recorded filter choices; importable on Python 3.8."""
import copy
import json
from pathlib import Path

LEGACY_LAYOUT = ['UNUSED', 'FC3', 'C3', 'O1', 'Oz', 'O2', 'C4', 'FC4']
LAYOUT = ['FC3', 'C3', 'O1', 'Pz', 'Oz', 'O2', 'C4', 'FC4']
RECORDED_LAYOUTS = {254: LEGACY_LAYOUT, 255: LAYOUT}
PRESETS = {
    'raw': dict(label='原始脑电（不滤波）', highpass_hz=None, lowpass_hz=None, notch_hz=[]),
    'general': dict(label='常规脑电：0.5–45 Hz / 50 Hz 陷波', highpass_hz=.5, lowpass_hz=45., notch_hz=[50.]),
    'motor': dict(label='运动节律：8–30 Hz / 50 Hz 陷波', highpass_hz=8., lowpass_hz=30., notch_hz=[50.]),
}
DEFAULT = dict(label='OmniBCI 脑电 · 8 个有效通道', port='/dev/omnibci-unconnected',
               serial='F8:5B:1B:F4:27:44', enabled=True, sample_rate_hz=250,
               labels=LAYOUT, disabled_channels=[], filter_preset='general', chunk_seconds=2)


def validate(item):
    from impedance_quality import acceptance_policy
    acceptance_policy(item)
    if item.get('filter_preset') not in PRESETS:
        raise ValueError('Unknown EEG filter preset')
    if item.get('labels') != LAYOUT or item.get('disabled_channels') != []:
        raise ValueError('This EEG profile requires active CH1–8: FC3 C3 O1 Pz Oz O2 C4 FC4')
    if item.get('sample_rate_hz') != 250 or item.get('chunk_seconds') != 2:
        raise ValueError('This EEG profile requires 250 Hz / 2 second chunks')
    return item


def apply(config, base):
    config = copy.deepcopy(config)
    item = dict(copy.deepcopy(DEFAULT), **config.get('eeg', {}))
    preferences = Path(base)/'runtime/eeg_preferences.json'
    if preferences.exists():
        choice = json.loads(preferences.read_text()).get('filter_preset')
        if choice not in PRESETS: raise ValueError('Invalid EEG preferences')
        item['filter_preset'] = choice
    config['eeg'] = validate(item)
    return config
