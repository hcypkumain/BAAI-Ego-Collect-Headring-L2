"""Enumerate every capture profile without changing the active profile."""
import json
import subprocess
import sys
import uuid
from pathlib import Path
from cloud_common import CloudError


def discover(executable=None):
    import project_paths
    from collection_profiles import MODES,profile_base,selection
    items=[];selection_value=selection(project_paths.CODE_BASE)
    for mode in MODES:
        expected=profile_base(project_paths.CODE_BASE,selection_value,mode)
        request=json.dumps({'mode':mode,'source':None}).encode()
        result=subprocess.run([executable or sys.executable,str(Path(__file__).with_name('usb_export_profile.py'))],
            input=request,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=120)
        if result.returncode:raise CloudError('无法扫描 '+mode+' 采集数据：'+result.stderr.decode('utf-8','replace')[:300])
        if len(result.stdout)>32*1024*1024:raise CloudError('采集批次列表过大')
        try:value=json.loads(result.stdout)
        except (ValueError,TypeError,RecursionError) as error:raise CloudError('采集批次列表无效') from error
        if (not isinstance(value,dict) or value.get('mode')!=mode or value.get('base')!=str(expected) or
                value.get('project')!=str(expected.parent) or not isinstance(value.get('sources'),list) or
                not isinstance(value.get('volume'),str)):
            raise CloudError('采集模式路径不匹配')
        volume=Path(value['volume']);seen=set()
        for raw in value['sources']:
            if not isinstance(raw,str):raise CloudError('采集路径无效')
            path=Path(raw)
            try:relative=path.relative_to(volume/'data')
            except ValueError as error:raise CloudError('采集目录不属于所选模式') from error
            if not path.is_absolute() or len(relative.parts)!=2 or raw in seen:raise CloudError('采集目录无效或重复')
            seen.add(raw);items.append(dict(mode=mode,source=raw,transaction_id=uuid.uuid4().hex))
    return items
