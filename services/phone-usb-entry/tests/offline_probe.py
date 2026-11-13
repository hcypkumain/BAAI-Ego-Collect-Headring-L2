#!/usr/bin/env python3
"""Run with python3 -I -S -B; no installs and no target access.

`blocked PATH_TO_FIXED_ENTRY` denies every socket/fork/spawn audit event.
`imports PATH_TO_PYTHON_CLOSURE` additionally permits ONLY /sbin/ldconfig -p
for unmodified ifaddr's ctypes libc lookup. Never run setup/build hooks.
"""
import ast
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys


def main():
    mode, path = sys.argv[1:]
    if mode not in ("blocked", "imports"):
        raise ValueError("explicit probe mode required")
    path = Path(path).resolve(strict=True)
    children = []

    def audit(event, args):
        if event.startswith("socket.") or event in ("os.fork", "os.forkpty", "os.system"):
            raise RuntimeError("forbidden action: " + event)
        if event in ("subprocess.Popen", "os.posix_spawn"):
            if mode != "imports" or list(args[1]) != ["/sbin/ldconfig", "-p"]:
                raise RuntimeError("forbidden child process")
            children.append({"event": event, "argv": list(args[1])})

    sys.addaudithook(audit)
    if mode == "blocked":
        spec = importlib.util.spec_from_file_location("fixed_entry", path)
        entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entry)
        for argv in ([], ["--publish"], ["--publish", "--duration", "600"],
                     ["--publish", "--duration", "600", "--check", "--selector", "/DO_NOT_READ"]):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                assert entry.main(argv) == 2
            assert json.loads(output.getvalue())["reason"] == "ISOLATION_UNAVAILABLE"
        print(json.dumps({"mode": mode, "cases": 4, "sockets": 0, "packets": 0, "children": children}))
    else:
        sys.path.insert(0, str(path))
        sys.modules["Cython"] = None
        import zeroconf
        import ifaddr
        import async_timeout
        for module in (zeroconf, ifaddr, async_timeout):
            Path(module.__file__).resolve().relative_to(path)
        sources = list(path.rglob("*.py"))
        for source in sources:
            text = source.read_text()
            ast.parse(text, feature_version=(3, 8))
            compile(text, str(source), "exec")
        licenses = [{"path": str(p.relative_to(path.parent)), "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                     "bytes": p.stat().st_size} for p in sorted((path.parent / "licenses").rglob("*")) if p.is_file()]
        print(json.dumps({"mode": mode, "python": sys.version, "zeroconf": zeroconf.__version__,
                          "python_files": len(sources), "licenses": licenses, "children_audit": children}, indent=2))


if __name__ == "__main__":
    main()
