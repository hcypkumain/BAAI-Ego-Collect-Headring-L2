import fcntl
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

COMMON = Path(__file__).resolve().parents[1] / 'src/orangepi_collect/common'
sys.path.insert(0, str(COMMON))

class LockTests(unittest.TestCase):
    def test_exclusive_delays_start_and_shared_readers_coexist(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'lock'; path.touch(mode=0o644)
            fd = os.open(path, os.O_RDONLY)
            fcntl.flock(fd, fcntl.LOCK_EX)
            code = "import sys,time; sys.path.insert(0,sys.argv[1]); from acquisition_time_lock import acquire; acquire(sys.argv[2]); print('locked',flush=True); time.sleep(60)"
            processes = [subprocess.Popen([sys.executable, '-c', code, str(COMMON), str(path)], stdout=subprocess.PIPE, text=True) for _ in range(2)]
            try:
                import select
                self.assertFalse(select.select([p.stdout for p in processes], [], [], .2)[0])
                fcntl.flock(fd, fcntl.LOCK_UN)
                for p in processes:
                    self.assertTrue(select.select([p.stdout], [], [], 5)[0])
                    self.assertEqual(p.stdout.readline().strip(), 'locked')
                with self.assertRaises(BlockingIOError): fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                for p in processes:
                    p.kill(); p.wait(); p.stdout.close()
                os.close(fd)

    def test_shared_lock_blocks_exclusive_until_process_death(self):
        from acquisition_time_lock import acquire
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'lock'; path.touch(mode=0o644)
            code = "import sys,time; sys.path.insert(0,sys.argv[1]); from acquisition_time_lock import acquire; acquire(sys.argv[2]); print('locked',flush=True); time.sleep(60)"
            proc = subprocess.Popen([sys.executable, '-c', code, str(COMMON), str(path)], stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(proc.stdout.readline().strip(), 'locked')
                fd = os.open(path, os.O_RDONLY)
                try:
                    with self.assertRaises(BlockingIOError): fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    proc.kill(); proc.wait(timeout=5)
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally: os.close(fd)
            finally:
                if proc.poll() is None: proc.kill(); proc.wait()
                proc.stdout.close()

    def test_shared_lock_has_bounded_wait(self):
        from acquisition_time_lock import acquire
        import time
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'lock';path.touch()
            with path.open('r') as f:
                fcntl.flock(f,fcntl.LOCK_EX)
                start=time.monotonic()
                with self.assertRaises(TimeoutError):acquire(path,timeout=.1)
                self.assertLess(time.monotonic()-start,1.0)

    def test_missing_or_symlink_fails_closed(self):
        from acquisition_time_lock import acquire
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            with self.assertRaises(OSError): acquire(path / 'missing')
            (path / 'target').touch(); (path / 'link').symlink_to(path / 'target')
            with self.assertRaises(OSError): acquire(path / 'link')
