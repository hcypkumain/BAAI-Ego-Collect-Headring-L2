"""Proxy recording metadata through a fresh process for the selected profile."""
import json
import subprocess
import sys
from pathlib import Path
from cloud_common import CloudError


class ProfileStore:
    def __init__(self, mode, source, *, executable=None):
        import project_paths
        from collection_profiles import MODES, profile_base, selection
        if mode not in MODES: raise CloudError('采集模式无效')
        self.source=Path(source)
        if not self.source.is_absolute():raise CloudError('采集目录必须是绝对路径')
        expected=profile_base(project_paths.CODE_BASE, selection(project_paths.CODE_BASE), mode)
        request=json.dumps(dict(mode=mode,source=str(self.source)),ensure_ascii=False).encode()
        process=subprocess.run([executable or sys.executable,str(Path(__file__).with_name('usb_export_profile.py'))],
            input=request,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=120)
        if process.returncode:raise CloudError('无法核验所选模式采集状态：'+process.stderr.decode('utf-8','replace')[:300])
        if len(process.stdout)>32*1024*1024:raise CloudError('采集状态结果过大')
        try:value=json.loads(process.stdout)
        except (ValueError,TypeError,RecursionError) as error:raise CloudError('采集状态结果无效') from error
        if (not isinstance(value,dict) or value.get('mode')!=mode or value.get('source')!=str(self.source) or
            value.get('base')!=str(expected) or value.get('project')!=str(expected.parent) or
            not isinstance(value.get('metadata'),dict)):
            raise CloudError('采集模式身份与结果不匹配')
        self.base=expected;self.project=expected.parent
        self.volume=Path(value['volume']);self.value=value['metadata']
        try:
            relative=self.source.relative_to(self.volume/'data')
            if len(relative.parts)!=2:raise ValueError()
        except ValueError as error:raise CloudError('采集源不属于所选模式') from error
        if self.value.get('id')!=self.source.name:raise CloudError('采集批次标识不匹配')

    def roots(self):return self.source.parent,self.volume
    def metadata(self,path):
        if Path(path)!=self.source:raise CloudError('采集批次已变化')
        return dict(self.value)
    def export_snapshot(self,path,metadata):
        if Path(path)!=self.source or not isinstance(self.value.get('daily_snapshot'),dict):raise CloudError('每日统计凭据缺失')
        return dict(self.value['daily_snapshot'])
