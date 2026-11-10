import importlib.util
import unittest
from pathlib import Path

SCRIPT=Path(__file__).resolve().parents[1]/'scripts/install_pi_services.py'
SPEC=importlib.util.spec_from_file_location('install_pi_services_renderer',SCRIPT)
MODULE=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

class SystemdUnitRendererTests(unittest.TestCase):
    def test_existing_suffixed_root_is_not_expanded_twice(self):
        raw=b'WorkingDirectory=/home/orangepi/Desktop/Embodied-Brain-Collect-main.2/orangepi\n'
        result=MODULE.render_systemd_unit(raw,Path('/srv/station.2'))
        self.assertEqual(result,b'WorkingDirectory=/srv/station.2/orangepi\n')
        self.assertNotIn(b'.2.2',result)

    def test_unsuffixed_legacy_root_is_rendered(self):
        raw=b'ExecStart=/home/orangepi/Desktop/Embodied-Brain-Collect-main/orangepi/scripts/run.sh service\n'
        result=MODULE.render_systemd_unit(raw,Path('/srv/station.2'))
        self.assertEqual(result,b'ExecStart=/srv/station.2/orangepi/scripts/run.sh service\n')

    def test_path_boundary_leaves_unrecognized_suffix_untouched(self):
        raw=b'X=/home/orangepi/Desktop/Embodied-Brain-Collect-main.2.2/tool\n'
        result=MODULE.render_systemd_unit(raw,Path('/srv/station.2'))
        self.assertEqual(result,raw)

    def test_exact_working_directory_root_without_trailing_slash_is_rendered(self):
        raw=b'WorkingDirectory=/home/orangepi/Desktop/Embodied-Brain-Collect-main\n'
        result=MODULE.render_systemd_unit(raw,Path('/srv/station.2'))
        self.assertEqual(result,b'WorkingDirectory=/srv/station.2\n')

    def test_mixed_legacy_roots_render_without_reprocessing_inserted_root(self):
        raw=(b'WorkingDirectory=/home/orangepi/Desktop/Embodied-Brain-Collect-main.2/orangepi\n'
             b'ExecStart=/home/orangepi/Desktop/Embodied-Brain-Collect-main/orangepi/run.sh\n')
        result=MODULE.render_systemd_unit(raw,Path('/srv/station.2'))
        self.assertEqual(result,b'WorkingDirectory=/srv/station.2/orangepi\nExecStart=/srv/station.2/orangepi/run.sh\n')

    def test_rendering_is_idempotent_when_project_root_contains_dot_two(self):
        raw=(b'WorkingDirectory=/home/orangepi/Desktop/Embodied-Brain-Collect-main.2/orangepi\n'
             b'ExecStart=/home/orangepi/Desktop/Embodied-Brain-Collect-main/orangepi/run.sh\n')
        root=Path('/srv/station.2')
        first=MODULE.render_systemd_unit(raw,root)
        self.assertEqual(MODULE.render_systemd_unit(first,root),first)

if __name__=='__main__':unittest.main()
