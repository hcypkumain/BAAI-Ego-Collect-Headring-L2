#!/usr/bin/env python3
"""Build an offline review payload from immutable Git + explicitly supplied archives.

No fetch, pip, setup.py, compiler, hooks, install, extraction, or service launch.
"""
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tarfile
import zipfile

SERVICE = "services/phone-usb-entry/"
SOURCES = (SERVICE + "fixed_entry.py", SERVICE + "requirements.lock.json", SERVICE + "README.md")
MAX_ARCHIVE = 1024 * 1024
MAX_EXPANDED = 4 * 1024 * 1024
MAX_MEMBERS = 2048


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_name(name):
    path = PurePosixPath(name)
    if (not name or "\\" in name or path.is_absolute() or
            any(p in ("", ".", "..") for p in name.rstrip("/").split("/"))):
        raise ValueError("unsafe archive member")
    return path


def no_symlinks(path):
    path = Path(path)
    if ".." in path.parts:
        raise ValueError("parent traversal refused")
    path = path.absolute()
    for item in (path,) + tuple(path.parents):
        if item.is_symlink():
            raise ValueError("symlink refused")
    return path


def read_archive(root, package):
    name = package["filename"]
    if Path(name).name != name:
        raise ValueError("archive basename required")
    source = no_symlinks(root / name)
    fd = os.open(str(source), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size != package["size"] or info.st_size > MAX_ARCHIVE:
            raise ValueError("archive size/type mismatch")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(MAX_ARCHIVE + 1)
    finally:
        os.close(fd)
    if digest(data) != package["sha256"]:
        raise ValueError("archive hash mismatch")
    return data


def select_members(data, package):
    """Read only Python package code + entire named license; never extract."""
    files = {}
    seen = set()
    expanded = 0
    prefix = package["python_prefix"]
    licenses = set(package["licenses"])
    found = set()

    def accept(name, size, is_file, is_dir, read):
        nonlocal expanded
        safe_name(name)
        if name in seen or len(seen) >= MAX_MEMBERS:
            raise ValueError("duplicate/member budget")
        seen.add(name)
        if not is_file and not is_dir:
            raise ValueError("links/special archive members refused")
        expanded += size
        if size < 0 or expanded > MAX_EXPANDED:
            raise ValueError("expanded budget")
        if not is_file:
            return
        if name in licenses:
            target = "licenses/" + package["name"] + "/" + PurePosixPath(name).name
            found.add(name)
        elif name.startswith(prefix) and name.endswith(".py"):
            # Keep the complete Python package (including upstream test_ifaddr),
            # not build scripts, extension sources, bytecode or host metadata.
            target = "python/" + prefix.rstrip("/").split("/")[-1] + "/" + name[len(prefix):]
        else:
            return
        content = read()
        if len(content) != size or target in files:
            raise ValueError("member content mismatch")
        files[target] = content

    if package["filename"].endswith(".whl"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for member in archive.infolist():
                mode = member.external_attr >> 16
                special = stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)
                accept(member.filename, member.file_size, not member.is_dir() and not special,
                       member.is_dir() and not special, lambda m=member: archive.read(m))
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for member in archive:
                accept(member.name, member.size, member.isfile(), member.isdir(),
                       lambda m=member: archive.extractfile(m).read(MAX_EXPANDED + 1))
    if found != licenses or not any(n.endswith("/__init__.py") for n in files):
        raise ValueError("incomplete Python/license closure")
    return files


def git_file(repo, ref, path):
    result = subprocess.run(["git", "--no-replace-objects", "-C", str(repo), "show", ref + ":" + path],
                            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
    if len(result.stdout) > MAX_ARCHIVE:
        raise ValueError("source budget")
    return result.stdout


def build(repo, ref, vendor_root, staging):
    if not re.fullmatch(r"[0-9a-f]{40}", ref):
        raise ValueError("immutable full commit SHA required")
    vendor_root = no_symlinks(vendor_root)
    staging = no_symlinks(staging)
    # mkdir is exclusive: never reuse/overwrite existing operator files.
    # Stage only in operator-owned directories, not system install prefixes.
    parent = staging.parent.stat()
    if (parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o022
            or str(staging).startswith(("/usr/", "/opt/", "/etc/", "/var/", "/bin/", "/sbin/"))):
        raise ValueError("new user staging directory required")
    kind = subprocess.run(["git", "--no-replace-objects", "-C", str(repo), "cat-file", "-t", ref],
                          check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
    if kind.stdout.strip() != b"commit":
        raise ValueError("commit object required")
    files = {"service/" + Path(p).name: git_file(repo, ref, p) for p in SOURCES}
    lock = json.loads(files["service/requirements.lock.json"])
    if lock.get("schema") != 1 or len(lock.get("packages", [])) != 3:
        raise ValueError("lock schema")
    for package in lock["packages"]:
        for name, content in select_members(read_archive(vendor_root, package), package).items():
            if name in files:
                raise ValueError("payload collision")
            files[name] = content
    manifest = {"schema": 1, "git_commit": ref, "state": "review-only-network-blocked",
                "requirements_sha256": digest(files["service/requirements.lock.json"]),
                "files": {p: {"sha256": digest(d), "size": len(d)} for p, d in sorted(files.items())}}
    files["MANIFEST.json"] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    tar_bytes = io.BytesIO()
    with tarfile.open(fileobj=tar_bytes, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, data in sorted(files.items()):
            safe_name(name)
            member = tarfile.TarInfo(name)
            member.size, member.mode, member.mtime = len(data), 0o600, 0
            member.uid = member.gid = 0
            archive.addfile(member, io.BytesIO(data))
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0) as stream:
        stream.write(tar_bytes.getvalue())
    data = output.getvalue()
    staging.mkdir(mode=0o700)
    for name, content in (("phone-usb-entry.tar.gz", data),
                          ("SHA256SUMS", (digest(data) + "  phone-usb-entry.tar.gz\n").encode())):
        fd = os.open(str(staging / name), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
    return digest(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--vendor-root", type=Path, required=True, help="external directory of exact locked official archives")
    parser.add_argument("--staging", type=Path, required=True, help="new user-owned staging directory")
    args = parser.parse_args()
    print(build(args.repo, args.ref, args.vendor_root, args.staging))


if __name__ == "__main__":
    main()
