"""Install collect's EEG-only dependencies plus the bundled platform SDK.

Run using the target machine's virtual-environment Python. --check only reports
compatibility and commands; it never installs anything.
"""
import argparse
import platform
from pathlib import Path
import subprocess
import struct
import sys


def select_wheel(root, system, machine):
    architecture = machine.strip().lower()
    operating_system = system.strip().lower()
    patterns = {
        ("windows", "amd64"): "*win_amd64.whl",
        ("windows", "x86_64"): "*win_amd64.whl",
        ("linux", "x86_64"): "*manylinux1_x86_64*.whl",
        ("linux", "amd64"): "*manylinux1_x86_64*.whl",
        ("linux", "aarch64"): "*manylinux2014_aarch64*.whl",
        ("linux", "arm64"): "*manylinux2014_aarch64*.whl",
    }
    pattern = patterns.get((operating_system, architecture))
    if pattern is None:
        raise RuntimeError(f"No bundled SDK for {system}/{machine}; need 64-bit Windows or glibc Linux x86_64/aarch64")
    wheels = list((root / "third_party" / "neurodance" / "whl").glob(pattern))
    if len(wheels) != 1:
        raise RuntimeError(f"Expected one bundled 9.8 SDK matching {pattern}, found {wheels}")
    return wheels[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    print(f"Interpreter: {sys.executable}", flush=True)
    print(f"Working directory: {Path.cwd()}", flush=True)
    print(f"Project directory: {root}", flush=True)
    print(f"Platform: system={platform.system()!r}, machine={platform.machine()!r}, bits={struct.calcsize('P') * 8}, libc={platform.libc_ver()}, Python={platform.python_version()}", flush=True)
    if platform.python_implementation() != "CPython" or sys.version_info < (3, 10):
        parser.error("collect requires standard CPython 3.10+ (3.10-3.13 recommended for these dependencies)")
    if struct.calcsize("P") != 8:
        parser.error("The bundled SDK requires 64-bit Python, regardless of kernel architecture")
    try:
        wheel = select_wheel(root, platform.system(), platform.machine())
    except RuntimeError as exc:
        parser.error(str(exc))
    print(f"SDK: {wheel.name}")
    commands = [
        [sys.executable, "-m", "pip", "install", "-r", str(root / "requirements-neurodance.txt"), str(wheel)],
        [sys.executable, "-m", "pip", "install", "--no-deps", "-e", str(root)],
    ]
    for command in commands:
        print(subprocess.list2cmdline(command), flush=True)
        if not args.check:
            subprocess.run(command, check=True)


if __name__ == "__main__":
    main()
