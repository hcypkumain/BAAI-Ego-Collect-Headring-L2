"""Local-only process harness; never imported by production code."""
from pathlib import Path
import tempfile
from unittest.mock import patch
import scan_registry

class OwnerTestSupport:
 def __init__(self):
  self.directory=tempfile.TemporaryDirectory();self.path=Path(self.directory.name)
  wrapper=self.path/'camera_owner_wrapper.py'
  wrapper.write_text("import os,sys;sys.path.insert(0,"+repr(str(Path(scan_registry.__file__).resolve().parent))+");from scan_registry import Registry;root,run,exe,*args=sys.argv[1:];Registry(root).register(run);os.environ['ORANGEPI_CAMERA_OWNER_ROOT']=root;os.environ['ORANGEPI_CAMERA_OWNER_RUN']=run;os.execv(exe,[exe]+args)")
  self.patch_path=patch.object(scan_registry,'WRAPPER_PATH',self.path/'camera_owner_wrapper.py')
  self.patch_validate=patch.object(scan_registry,'validate_owned_command',lambda *args:None)
 def start(self):self.patch_path.start();self.patch_validate.start();return self
 def stop(self):self.patch_validate.stop();self.patch_path.stop();self.directory.cleanup()
