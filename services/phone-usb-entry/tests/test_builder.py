"""Archive rejection tests; no install, hooks or network."""
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from test_fixed_entry import builder, HERE


class BuilderTests(unittest.TestCase):
    def package(self, **changes):
        value = {"filename": "example.tar.gz", "name": "example", "python_prefix": "example/",
                 "licenses": ["LICENSE"]}
        value.update(changes)
        return value

    def tar(self, members):
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode="w:gz") as archive:
            for name, kind, content in members:
                info = tarfile.TarInfo(name)
                if kind == "link":
                    info.type = tarfile.SYMTYPE
                    info.linkname = "/etc/passwd"
                    archive.addfile(info)
                else:
                    info.size = len(content)
                    archive.addfile(info, io.BytesIO(content))
        return out.getvalue()

    def test_whitelist_full_license_no_build_hooks(self):
        data = self.tar([("example/__init__.py", "file", b"# synthetic\n"),
                         ("LICENSE", "file", b"complete synthetic license\n"),
                         ("setup.py", "file", b"raise AssertionError('must not execute')"),
                         ("example/native.so", "file", b"not Python"),
                         ("example/build.pyx", "file", b"not Python")])
        result = builder.select_members(data, self.package())
        self.assertEqual(set(result), {"python/example/__init__.py", "licenses/example/LICENSE"})
        self.assertEqual(result["licenses/example/LICENSE"], b"complete synthetic license\n")

    def test_tar_paths_links_duplicate_license_budget(self):
        base = [("example/__init__.py", "file", b""), ("LICENSE", "file", b"full")]
        for malicious in [("../escape", "file", b"x"), ("/escape", "file", b"x"),
                          ("a/../../escape", "file", b"x"), ("a\\escape", "file", b"x"),
                          ("x", "link", b""), ("LICENSE", "file", b"duplicate")]:
            with self.subTest(malicious=malicious), self.assertRaises(ValueError):
                builder.select_members(self.tar(base + [malicious]), self.package())
        with self.assertRaises(ValueError):
            builder.select_members(self.tar(base[:1]), self.package())
        with patch.object(builder, "MAX_EXPANDED", 2), self.assertRaises(ValueError):
            builder.select_members(self.tar(base), self.package())
        with patch.object(builder, "MAX_MEMBERS", 1), self.assertRaises(ValueError):
            builder.select_members(self.tar(base), self.package())

    def test_zip_symlink_refused(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            info = zipfile.ZipInfo("example/link.py")
            info.create_system = 3
            info.external_attr = 0o120777 << 16
            archive.writestr(info, "/etc/passwd")
        with self.assertRaises(ValueError):
            builder.select_members(buffer.getvalue(), self.package(filename="example.whl"))

    def test_hash_type_symlink_and_immutable_ref_rejection(self):
        with tempfile.TemporaryDirectory(prefix="8788-builder-", dir="/tmp/opencode") as temp:
            root = Path(temp)
            archive = root / "example.tar.gz"
            archive.write_bytes(b"bad")
            pkg = self.package(size=3, sha256="0" * 64)
            with self.assertRaises(ValueError):
                builder.read_archive(root, pkg)
            archive.unlink()
            archive.symlink_to("/etc/passwd")
            with self.assertRaises(ValueError):
                builder.read_archive(root, pkg)
            with self.assertRaises(ValueError):
                builder.build(root, "HEAD", root, root / "new-stage")
            self.assertFalse((root / "new-stage").exists())

    def test_lock_exact_transitive_closure(self):
        lock = json.loads((HERE / "requirements.lock.json").read_text())
        self.assertEqual([(p["name"], p["version"]) for p in lock["packages"]],
                         [("zeroconf", "0.136.2"), ("ifaddr", "0.2.0"), ("async-timeout", "4.0.3")])
        for package in lock["packages"]:
            self.assertTrue(package["url"].startswith("https://files.pythonhosted.org/packages/"))
            self.assertEqual(len(package["sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
