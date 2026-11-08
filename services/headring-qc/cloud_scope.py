"""Hard boundaries for administrator cloud credentials. No network at import."""
from pathlib import PurePosixPath

ALLOWED_ROOTS=frozenset(('EGOL2','EGOL3_hand'))
DELETE_ROOT='EGOL2'
BUCKET='baai-eai-datasets'
OBJECT_ROOT='data'

def _parts(value):
    if (not isinstance(value,str) or not value or '\\' in value or '%' in value
            or '\x00' in value or value.startswith('/') or '//' in value):
        raise ValueError('Invalid cloud path')
    path=PurePosixPath(value)
    if '..' in path.parts or str(path)!=value:raise ValueError('Noncanonical cloud path')
    return path.parts

def dataset_path(value):
    parts=_parts(value)
    if len(parts)<2 or parts[0] not in ALLOWED_ROOTS:raise ValueError('Cloud dataset outside allowed roots')
    return value

def object_key(dataset,relative):
    dataset_path(dataset);parts=_parts(relative)
    if parts[0] not in ('audios','data','meta','videos'):raise ValueError('Not a standard package file')
    return OBJECT_ROOT+'/'+dataset+'/'+relative

def scoped_key(key,delete=False):
    parts=_parts(key[:-1] if isinstance(key,str) and key.endswith('/') else key)
    if len(parts)<3 or parts[0]!=OBJECT_ROOT or parts[1] not in ALLOWED_ROOTS:
        raise ValueError('Cloud object outside allowed roots')
    if delete and parts[1]!=DELETE_ROOT:raise ValueError('Cloud deletion is authorized ONLY in EGOL2')
    return key

def scoped_prefix(prefix):
    if not isinstance(prefix,str) or not prefix.endswith('/'):raise ValueError('Directory prefix requires trailing slash')
    base=prefix[:-1];parts=_parts(base)
    if len(parts)<2 or parts[0]!=OBJECT_ROOT or parts[1] not in ALLOWED_ROOTS:
        raise ValueError('Cloud listing prefix outside allowed roots')
    return prefix

def exact_file_prefix(key):
    scoped_key(key);return key

def validate_listing(rows,prefix):
    scoped_prefix(prefix);seen=set()
    for row in rows:
        name=row['key']
        if not isinstance(name,str) or not name.startswith(prefix):raise ValueError('Cloud server returned out-of-scope object')
        if name in seen:raise ValueError('Duplicate cloud object')
        seen.add(name)
        if name!=prefix:scoped_key(name)
        if type(row.get('bytes')) is not int or row['bytes']<0:raise ValueError('Invalid cloud object size')
    return rows

def deletion_plan(rows):
    prefix='data/EGOL2/';validate_listing(rows,prefix)
    return [dict(row) for row in rows if row['key']!=prefix and scoped_key(row['key'],delete=True)]

def destination_for_profile(profile,episode_name):
    if profile not in ('L2','L3'):raise ValueError('Only complete fixed L2/L3 profiles may upload')
    if len(_parts(episode_name))!=1:raise ValueError('Episode cloud name must be one path component')
    return dataset_path(('EGOL2' if profile=='L2' else 'EGOL3_hand')+'/'+episode_name)

def expected_standard_files(profile):
    if profile not in ('L2','L3'):raise ValueError('Unknown fixed standard profile')
    cameras=['headband_left_rgb','headband_right_rgb','headband_bleft_rgb',
             'headband_bright_rgb','left_wrist_rgb','right_wrist_rgb']
    features=['observation.images.'+name for name in cameras]
    features+=['observation.'+side+'_wrist_'+kind for side in ('left','right') for kind in ('emg','imu')]
    features+=['observation.board_imu','observation.marker']
    if profile=='L3':features+=['observation.eeg','observation.eeg_impedance']
    meta=['info.json','episodes.jsonl','episodes_stats.jsonl','tasks.jsonl',
          'qc_reports.jsonl','alignment_check.jsonl','calibrations.jsonl','collect_info.jsonl']
    return ({'audios/.keep'}|{'data/chunk-000/episode_000000/'+name+'.parquet' for name in features+['master_index']}
        |{'videos/chunk-000/observation.images.'+name+'/episode_000000.mp4' for name in cameras}
        |{'meta/'+name for name in meta})
