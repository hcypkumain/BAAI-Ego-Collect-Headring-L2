import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/control'))
from diagnostic_events import EventWriter, export_events


class DiagnosticEventsTests(unittest.TestCase):
    def test_rotation_permissions_and_sanitized_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = EventWriter(Path(tmp)/'diagnostics', max_bytes=180, files=4)
            for _ in range(15):writer.emit('action_complete', action='start', ok=False,
                request_id='a'*32, reason='failed', secret='sensitive', payload={'token':'sensitive'})
            writer.queue.join()
            directory=Path(tmp)/'diagnostics'
            self.assertEqual(os.stat(directory).st_mode & 0o777, 0o700)
            files=list(directory.iterdir())
            self.assertLessEqual(len(files),4)
            self.assertTrue(all(os.stat(f).st_mode & 0o777 == 0o600 for f in files))
            self.assertTrue(all(f.stat().st_size<=180 for f in files))
            with (directory/'events.jsonl').open('ab') as stream:
                stream.write(b'{"event":"action_complete","action":"password","secret":"sensitive"}\n')
            data=export_events(directory)
            self.assertNotIn(b'sensitive',data)
            self.assertNotIn(b'password',data)
            self.assertTrue(all(isinstance(json.loads(line),dict) for line in data.splitlines()))

    def test_full_queue_and_broken_writer_never_raise(self):
        writer=EventWriter.__new__(EventWriter)
        writer.queue=queue.Queue(maxsize=1);writer.dropped=0
        writer.emit('action_taken', action='stop')
        writer.emit('action_taken', action='stop')
        self.assertEqual(writer.dropped,1)
        with tempfile.TemporaryDirectory() as tmp:
            broken=Path(tmp)/'file';broken.write_text('x')
            writer=EventWriter(broken/'child')
            writer.emit('action_taken', action='stop')
            writer.queue.join()
            self.assertEqual(writer.dropped,1)


if __name__ == '__main__':unittest.main()
