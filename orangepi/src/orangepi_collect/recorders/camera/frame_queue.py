"""Lossless camera handoff bounded by both frame count and payload bytes."""
import queue
import time


class FrameQueue(queue.Queue):
    def __init__(self, maxsize, max_bytes):
        super().__init__(maxsize)
        self.max_bytes = max_bytes
        self.payload_bytes = self.peak_bytes = self.peak_frames = 0

    @staticmethod
    def weight(item):
        return 0 if item is None else len(item[0])

    def put(self, item, block=True, timeout=None):
        size = self.weight(item)
        if size > self.max_bytes:
            raise queue.Full
        with self.not_full:
            def full():
                return self._qsize() >= self.maxsize or self.payload_bytes + size > self.max_bytes
            if not block:
                if full():
                    raise queue.Full
            elif timeout is None:
                while full():
                    self.not_full.wait()
            elif timeout < 0:
                raise ValueError('timeout must be non-negative')
            else:
                deadline = time.monotonic() + timeout
                while full():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise queue.Full
                    self.not_full.wait(remaining)
            self._put(item)
            self.payload_bytes += size
            self.peak_bytes = max(self.peak_bytes, self.payload_bytes)
            self.peak_frames = max(self.peak_frames, self._qsize())
            self.unfinished_tasks += 1
            self.not_empty.notify()

    def _get(self):
        item = super()._get()
        self.payload_bytes -= self.weight(item)
        return item

    def stats(self):
        with self.mutex:
            return dict(frames=self._qsize(), bytes=self.payload_bytes,
                        peak_frames=self.peak_frames, peak_bytes=self.peak_bytes,
                        capacity_frames=self.maxsize, capacity_bytes=self.max_bytes)
