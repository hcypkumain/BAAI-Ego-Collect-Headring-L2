import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("neurodance_installer", ROOT / "scripts/install_neurodance.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


@pytest.mark.parametrize("system,machine,suffix", [
    ("Linux", "aarch64", "manylinux2014_aarch64"),
    (" linux ", " ARM64 ", "manylinux2014_aarch64"),
    ("Windows", "AMD64", "win_amd64.whl"),
])
def test_selection_independent_of_working_directory(system, machine, suffix, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert suffix in installer.select_wheel(ROOT, system, machine).name


def test_unsupported_architecture_not_forced():
    with pytest.raises(RuntimeError, match="Linux/armv7l"):
        installer.select_wheel(ROOT, "Linux", "armv7l")
