import ast
import types
import unittest
from pathlib import Path
from unittest.mock import Mock

source=Path(__file__).resolve().parents[1]/'dynamic_guard.py'
tree=ast.parse(source.read_text())
fn=next(x for x in tree.body if isinstance(x,ast.FunctionDef) and x.name=='find_interface')
module=ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[]))
class Refused(Exception): pass
namespace={'guard':types.SimpleNamespace(Refused=Refused)}
exec(compile(module,str(source),'exec'),namespace)
find=namespace['find_interface']
class Tests(unittest.TestCase):
 def make(self,names,matches):
  fs=types.SimpleNamespace(root=types.SimpleNamespace(iterdir=lambda:[types.SimpleNamespace(name=n) for n in names]))
  fs.matches=lambda name,selector:name in matches
  return fs
 def test_selector_tracks_actual_name(self):
  self.assertEqual(find({'selector':{}},self.make(['usb0','wlan0'],{'usb0'})),'usb0')
  self.assertEqual(find({'selector':{}},self.make(['usb1','usb0'],{'usb1'})),'usb1')
 def test_absent_and_ambiguous_are_rejected(self):
  self.assertIsNone(find({'selector':{}},self.make(['usb0'],set())))
  self.assertIsNone(find({'selector':{}},self.make(['usb0','usb1'],{'usb0','usb1'})))
 def test_enumeration_bound(self):
  self.assertIsNone(find({'selector':{}},self.make([str(i) for i in range(257)],{'1'})))
