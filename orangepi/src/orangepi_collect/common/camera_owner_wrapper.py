"""Register a newly created session before executing any camera-owning code."""
import os
import sys
from scan_registry import Registry, RegistryError, validate_owned_command, canonical_runtime

if __name__=='__main__':
    root,run,executable,*args=sys.argv[1:]
    if __import__('pathlib').Path(root).resolve()!=canonical_runtime():raise RegistryError('设备所有权目录无效')
    registry=Registry(root);value=registry.inspect()
    if not value or value.get('run')!=run:raise RegistryError('设备所有权已变化')
    validate_owned_command(value.get('kind'),[executable,*args])
    registry.register(run)
    os.environ['ORANGEPI_CAMERA_OWNER_ROOT']=root
    os.environ['ORANGEPI_CAMERA_OWNER_RUN']=run
    os.execv(executable,[executable]+args)
