import sys
from pathlib import Path
import queue
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import base64
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'))
import project_paths
from orangepi_collect.recorders.camera.uvc_source import validate_config
from orangepi_collect.recorders.camera.mpp_camera_recorder import Camera


class WristGateTests(unittest.TestCase):
    def setUp(self):
        self.jpeg = base64.b64decode(
            '/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAABAAEDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD5/ooooA//2Q==')
        patcher = patch('orangepi_collect.recorders.camera.mpp_camera_recorder.publisher')
        patcher.start()
        self.addCleanup(patcher.stop)

    def camera(self):
        camera=Camera.__new__(Camera)
        camera.owner=SimpleNamespace(gate=threading.RLock(),active=True,start_ns=100,fail=Mock())
        camera.seen=0;camera.name='cam_left_wrist';camera.queue=queue.Queue(maxsize=1)
        camera.capture_size=(1,1);camera.observed_size=(1,1)
        return camera

    def test_delayed_prestart_buffer_is_not_recorded(self):
        camera=self.camera()
        camera.usb_callback(self.jpeg,1,0,120,1000,99)
        self.assertTrue(camera.queue.empty())
        camera.usb_callback(self.jpeg,2,1,140,1020,110)
        self.assertEqual(camera.queue.get_nowait(),(self.jpeg,1020,2,140,1020))

    def test_stop_gate_prevents_new_frames(self):
        camera=self.camera();camera.owner.active=False
        camera.usb_callback(self.jpeg,1,0,120,1000,110)
        self.assertTrue(camera.queue.empty())

    def test_overflow_fails_instead_of_silent_frame_loss(self):
        camera=self.camera()
        camera.usb_callback(self.jpeg,1,0,120,1000,110)
        camera.usb_callback(self.jpeg,2,1,140,1020,130)
        camera.owner.fail.assert_called_once()

    def test_unsupported_format_and_rate_are_rejected(self):
        base=dict(device='/dev/video84',width=640,height=480,fps=30,fourcc='MJPG')
        for changed in [dict(fourcc='YUYV'),dict(fps=0),dict(width=-1),dict(device='0')]:
            with self.assertRaises(ValueError):validate_config(dict(base,**changed))


if __name__=='__main__':unittest.main()
