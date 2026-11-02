"""Physiological wristband recorder over BLE (protocol V1.5).

Bleak is asynchronous, while the collection framework expects synchronous
``_open/_poll/_close`` methods.  A small background thread owns Bleak's asyncio
event loop and puts notification chunks into a thread-safe queue.  The recorder
thread performs byte-stream reassembly, parsing and storage.
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time
from typing import Any

import numpy as np

from ..base import BaseRecorder
from .protocol import (
    ACCEL_SCALE_M_S2,
    GYRO_SCALE_RAD_S,
    DataFrame,
    FrameStreamDecoder,
    StatusFrame,
    TimeResponseFrame,
    build_time_sync_frame,
    parse_frame,
)
from .wristband_recorder_config import WristbandRecorderConfig


class WristbandRecorder(BaseRecorder):
    """Receive, timestamp and save one physiological wristband."""

    name = "wristband"
    output_dir = "wristband"
    config: WristbandRecorderConfig

    def __init__(self, config: WristbandRecorderConfig):
        super().__init__(config)

        # _rx_queue 在 _reset_for_open() 里创建:queue.Queue 含线程锁,
        # 放在 __init__ 会无法跨进程 pickle(launcher spawn 时直接传对象)。
        self._decoder = FrameStreamDecoder()

        self._worker_thread: threading.Thread | None = None
        self._worker_stop = threading.Event()
        self._worker_finished = threading.Event()
        self._connected = threading.Event()
        self._unexpected_disconnect = threading.Event()
        self._worker_error = ""

        self._time_sync_ack = threading.Event()
        self._time_sync_failure = ""
        self._time_sync_command: bytes | None = None
        self._time_sync_sent_unix_s: float | None = None
        self._time_sync_sent_monotonic: float | None = None
        self._time_sync_ack_unix_s: float | None = None

        self._selected_device_name = ""
        self._selected_device_address = ""
        self._recording = False
        self._record_start_unix_s: float | None = None
        self._record_end_unix_s: float | None = None

        self._last_sequence: int | None = None
        self._data_frames_seen = 0
        self._data_frames_saved = 0
        self._status_frames_saved = 0
        self._dropped_frames = 0
        self._duplicate_frames = 0
        self._invalid_timestamps = 0
        self._parse_errors = 0
        self._queue_dropped_chunks = 0

    # ------------------------------------------------------------------
    # Framework lifecycle
    # ------------------------------------------------------------------

    def _open(self) -> bool:
        """Start BLE, connect, and complete the 0x20 UTC sync handshake."""

        try:
            import bleak  # noqa: F401  # imported lazily for clearer errors
        except ImportError:
            self._open_error = (
                "BLE dependency is missing; run `pip install bleak` or "
                "`pip install -r requirements.txt`"
            )
            self._log(f"[wristband] open failed — {self._open_error}")
            return False

        self._reset_for_open()
        self._log(
            "[wristband] scanning for BLE device "
            f"(service={self.config.service_uuid}) ..."
        )
        self._worker_thread = threading.Thread(
            target=self._worker_main,
            name="wristband-ble",
            daemon=True,
        )
        self._worker_thread.start()

        deadline = time.monotonic() + max(1.0, self.config.open_timeout)
        while time.monotonic() < deadline:
            self._drain_notifications(block_timeout=0.05, raise_worker_error=False)

            if self._time_sync_failure:
                return self._fail_open(self._time_sync_failure)
            if self._worker_error:
                return self._fail_open(self._worker_error)
            # UTC sync ack = 协议握手完成,即 open 就绪;0x15 数据流是否
            # 在发由 launcher 的确认阶段判断
            if self._time_sync_ack.is_set():
                self._log("[wristband] UTC sync acknowledged — ready")
                return True

            now = time.monotonic()
            if (
                self._time_sync_sent_monotonic is not None
                and not self._time_sync_ack.is_set()
                and now - self._time_sync_sent_monotonic > self.config.sync_timeout
            ):
                return self._fail_open(
                    f"no valid 0x20 UTC-sync acknowledgement within "
                    f"{self.config.sync_timeout:g}s"
                )

        return self._fail_open(
            f"device was not ready within {self.config.open_timeout:g}s"
        )

    def _record(self) -> None:
        # The device may keep pre-session samples in its one-second cache;
        # the UTC window (armed here, re-armed at commit) drops them.
        self._reset_stream_state()
        try:
            super()._record()
        finally:
            self._recording = False

    def _reset_stream_state(self) -> None:
        """挂 UTC 窗口并归零计数。开录与 commit 各走一次:预热期的帧经
        窗口判为过期丢弃,buffer 由基类清空,计数器与落盘内容保持一致。"""
        self._record_start_unix_s = time.time()
        self._record_end_unix_s = None
        self._last_sequence = None
        self._recording = True
        self._data_frames_saved = 0
        self._status_frames_saved = 0
        self._dropped_frames = 0
        self._duplicate_frames = 0

    def _loop(self) -> None:
        """Normal collection loop plus a bounded end-of-session tail drain."""

        self._loop_t0 = self._record_start_unix_s or time.time()
        last_heartbeat = self._loop_t0

        while True:
            if self._maybe_commit():
                last_heartbeat = self._loop_t0   # commit 重挂了 UTC 窗口
            now = time.time()
            elapsed = now - (self._record_start_unix_s or self._loop_t0)
            if self._stop_by_duration(elapsed) or self.stop_event.is_set():
                break
            self._poll(now)
            if now - last_heartbeat > 0.5:
                last_heartbeat = now
                self._heartbeat(elapsed)

        # Frames sampled just before stop can arrive in the next 1 Hz burst.
        # Keep receiving briefly, but _store_data_frame filters out sample
        # timestamps later than this boundary.
        self._record_end_unix_s = time.time()
        tail_wait = max(0.0, self.config.tail_wait)
        if tail_wait:
            self._log(
                f"[wristband] session stopped; draining final BLE batch for "
                f"up to {tail_wait:g}s"
            )
            tail_deadline = time.monotonic() + tail_wait
            while time.monotonic() < tail_deadline:
                try:
                    self._poll(time.time())
                except RuntimeError as exc:
                    self._log(f"[wristband] tail drain ended early — {exc}")
                    break

    def _poll(self, ts: float) -> None:
        # ``ts`` is deliberately not used as the scientific sample time.  BLE
        # delivers one-second bursts, so device UTC in each 0x15 frame is the
        # authoritative time source.
        del ts
        self._drain_notifications(block_timeout=0.05, raise_worker_error=True)

    def _close(self) -> None:
        self._worker_stop.set()
        thread = self._worker_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        self._worker_thread = None
        self._log(
            f"[wristband] stopped (saved_frames={self._data_frames_saved}, "
            f"drop={self._dropped_frames}, duplicate={self._duplicate_frames}, "
            f"bad={self._bad_frame_count()})"
        )

    # ------------------------------------------------------------------
    # BLE worker (background asyncio thread)
    # ------------------------------------------------------------------

    def _worker_main(self) -> None:
        try:
            asyncio.run(self._ble_session())
        except Exception as exc:
            if not self._worker_stop.is_set():
                self._worker_error = f"{type(exc).__name__}: {exc}"
        finally:
            self._worker_finished.set()

    async def _ble_session(self) -> None:
        from bleak import BleakClient

        device = await self._discover_device()
        self._selected_device_name = getattr(device, "name", None) or ""
        self._selected_device_address = getattr(device, "address", "") or ""
        self._log(
            f"[wristband] connecting to "
            f"{self._selected_device_name or '(unnamed)'} "
            f"[{self._selected_device_address}] ..."
        )

        def on_disconnect(_client: Any) -> None:
            if not self._worker_stop.is_set():
                self._unexpected_disconnect.set()

        client = BleakClient(
            device,
            disconnected_callback=on_disconnect,
            timeout=self.config.connect_timeout,
        )
        notify_started = False
        try:
            await client.connect()
            if not client.is_connected:
                raise ConnectionError("BLE connection did not become active")

            notify_char = client.services.get_characteristic(self.config.notify_uuid)
            write_char = client.services.get_characteristic(self.config.write_uuid)
            if notify_char is None:
                raise RuntimeError(
                    f"notify characteristic not found: {self.config.notify_uuid}"
                )
            if write_char is None:
                raise RuntimeError(
                    f"write characteristic not found: {self.config.write_uuid}"
                )

            await client.start_notify(notify_char, self._on_notification)
            notify_started = True

            # Build immediately before the write so the millisecond field is as
            # close as practical to the time observed by the firmware.
            command = build_time_sync_frame(sequence=0)
            self._time_sync_command = command
            self._time_sync_sent_unix_s = time.time()
            self._time_sync_sent_monotonic = time.monotonic()

            response = self.config.write_with_response
            if response is None:
                properties = {
                    str(p).lower().replace("_", "-")
                    for p in getattr(write_char, "properties", [])
                }
                response = (
                    "write" in properties and "write-without-response" not in properties
                )
            await client.write_gatt_char(
                write_char,
                command,
                response=bool(response),
            )
            self._connected.set()
            self._log("[wristband] BLE connected; 0x20 UTC sync sent")

            while not self._worker_stop.is_set():
                if self._unexpected_disconnect.is_set() or not client.is_connected:
                    raise ConnectionError("BLE device disconnected")
                await asyncio.sleep(0.05)
        finally:
            if notify_started and client.is_connected:
                try:
                    await client.stop_notify(self.config.notify_uuid)
                except Exception:
                    pass
            if client.is_connected:
                try:
                    await client.disconnect()
                except Exception:
                    pass

    async def _discover_device(self):
        from bleak import BleakScanner

        found = await BleakScanner.discover(
            timeout=self.config.scan_timeout,
            return_adv=True,
        )
        if isinstance(found, dict):
            entries = list(found.values())
        else:  # Compatibility fallback for older/custom Bleak backends.
            entries = [(device, None) for device in found]

        wanted_address = self.config.address.strip().lower()
        wanted_name = self.config.device_name.strip().lower()
        wanted_service = self.config.service_uuid.strip().lower()
        matches: list[tuple[Any, Any]] = []

        for device, advertisement in entries:
            address = (getattr(device, "address", "") or "").lower()
            local_name = (
                getattr(advertisement, "local_name", None)
                or getattr(device, "name", None)
                or ""
            )
            services = {
                str(uuid).lower()
                for uuid in (getattr(advertisement, "service_uuids", None) or [])
            }

            if wanted_address:
                matched = address == wanted_address
            elif wanted_name:
                matched = wanted_name in local_name.lower()
            else:
                matched = wanted_service in services
            if matched:
                matches.append((device, advertisement))

        if not matches:
            visible = []
            for device, advertisement in entries[:20]:
                local_name = (
                    getattr(advertisement, "local_name", None)
                    or getattr(device, "name", None)
                    or "(unnamed)"
                )
                visible.append(f"{local_name}[{getattr(device, 'address', '?')}]")
            selector = (
                f"address={self.config.address!r}"
                if wanted_address
                else f"name contains {self.config.device_name!r}"
                if wanted_name
                else f"advertised service={self.config.service_uuid}"
            )
            suffix = "; visible: " + ", ".join(visible) if visible else ""
            raise RuntimeError(f"no wristband matched {selector}{suffix}")

        # If several identical devices are present and no explicit selector was
        # supplied, choose the strongest advertisement.  For formal collection
        # the printed address should then be copied into the config.
        def signal_strength(item: tuple[Any, Any]) -> int:
            rssi = getattr(item[1], "rssi", -999) if item[1] is not None else -999
            return int(rssi) if rssi is not None else -999

        matches.sort(key=signal_strength, reverse=True)
        return matches[0][0]

    def _on_notification(self, _sender: Any, data: bytearray) -> None:
        item = (time.time(), bytes(data))
        try:
            self._rx_queue.put_nowait(item)
        except queue.Full:
            self._queue_dropped_chunks += 1

    # ------------------------------------------------------------------
    # Stream decoding and protocol handling
    # ------------------------------------------------------------------

    def _drain_notifications(
        self,
        *,
        block_timeout: float,
        raise_worker_error: bool,
    ) -> None:
        chunks: list[tuple[float, bytes]] = []
        try:
            chunks.append(self._rx_queue.get(timeout=block_timeout))
        except queue.Empty:
            pass
        while True:
            try:
                chunks.append(self._rx_queue.get_nowait())
            except queue.Empty:
                break

        for host_received_s, chunk in chunks:
            for raw_frame in self._decoder.feed(chunk):
                self._handle_complete_frame(raw_frame, host_received_s)

        if raise_worker_error and self._worker_error:
            raise RuntimeError(self._worker_error)
        if (
            raise_worker_error
            and self._unexpected_disconnect.is_set()
            and not self._worker_stop.is_set()
        ):
            raise RuntimeError("BLE device disconnected")

    def _handle_complete_frame(self, raw_frame: bytes, host_received_s: float) -> None:
        try:
            parsed = parse_frame(raw_frame)
        except ValueError:
            self._parse_errors += 1
            return

        if isinstance(parsed, TimeResponseFrame):
            if (
                self._time_sync_command is not None
                and parsed.raw == self._time_sync_command
            ):
                if not self._time_sync_ack.is_set():
                    self._time_sync_ack_unix_s = host_received_s
                    self._time_sync_ack.set()
                    self._log("[wristband] 0x20 UTC sync acknowledged")
            elif parsed.is_failure:
                self._time_sync_failure = "device rejected the 0x20 UTC sync command"
            return

        if isinstance(parsed, StatusFrame):
            if self._recording and self._host_time_in_window(host_received_s):
                self._store_status_frame(parsed, host_received_s)
            return

        self._data_frames_seen += 1
        if not parsed.timestamp_valid and self._recording:
            self._invalid_timestamps += 1
            return

        if self._recording and parsed.timestamp_valid:
            self._store_data_frame(parsed, host_received_s)

    def _store_data_frame(self, frame: DataFrame, host_received_s: float) -> None:
        t0 = frame.timestamp_ms / 1000.0
        start = self._record_start_unix_s
        end = self._record_end_unix_s
        if start is None:
            return

        # The latest sample in a frame is pressure P2 at T + 2/150 s.
        if t0 + 2.0 / 150.0 < start:
            self._last_sequence = frame.sequence
            return
        if end is not None and t0 > end:
            return
        if not self._accept_sequence(frame.sequence):
            return

        self._data_frames_saved += 1
        self._acc("frame_timestamp_unix_ms", frame.timestamp_ms)
        self._acc("frame_timestamps", t0)
        self._acc("host_receive_timestamps", host_received_s)
        self._acc("frame_sequence", frame.sequence)
        if self.config.store_raw_packets:
            self._acc_arr(
                "raw_packets",
                np.frombuffer(frame.raw, dtype=np.uint8).copy(),
            )

        pressure_ts = (t0, t0 + 1.0 / 150.0, t0 + 2.0 / 150.0)
        for sample_ts, value in zip(pressure_ts, frame.pressure_raw):
            if self._sample_time_in_window(sample_ts):
                self._acc("pressure_timestamps", sample_ts)
                self._acc("pressure_raw", value)

        ppg_ts = (t0, t0 + 1.0 / 100.0)
        for sample_ts, values in zip(ppg_ts, frame.ppg_raw):
            if self._sample_time_in_window(sample_ts):
                self._acc("ppg_timestamps", sample_ts)
                self._acc_arr("ppg_raw", np.asarray(values, dtype=np.int32))

        if self._sample_time_in_window(t0):
            self._acc("imu_timestamps", t0)
            self._acc_arr("accel_raw", np.asarray(frame.accel_raw, dtype=np.int16))
            self._acc_arr("accel_m_s2", np.asarray(frame.accel_m_s2, dtype=np.float32))
            self._acc_arr("gyro_raw", np.asarray(frame.gyro_raw, dtype=np.int16))
            self._acc_arr("gyro_rad_s", np.asarray(frame.gyro_rad_s, dtype=np.float32))

            # These values are intentionally stored for every 0x15 frame.  The
            # firmware updates them at 1 Hz and holds/repeats the most recent
            # value in the other 49 frames; no "new value" flag exists.
            self._acc("temperature_timestamps", t0)
            self._acc("temperature_raw", frame.temperature_raw)
            self._acc(
                "temperature_c",
                np.nan if frame.temperature_c is None else frame.temperature_c,
            )
            self._acc("spo2_timestamps", t0)
            self._acc("spo2_raw", frame.spo2_raw)
            self._acc(
                "spo2_percent",
                np.nan if frame.spo2_percent is None else frame.spo2_percent,
            )

    def _store_status_frame(self, frame: StatusFrame, host_received_s: float) -> None:
        self._status_frames_saved += 1
        self._acc("status_host_timestamps", host_received_s)
        self._acc("battery_raw", frame.battery_raw)
        self._acc(
            "battery_percent",
            np.nan if frame.battery_percent is None else frame.battery_percent,
        )
        self._acc("storage_remaining_raw", frame.storage_remaining_raw)
        self._acc(
            "storage_remaining_percent",
            np.nan
            if frame.storage_remaining_percent is None
            else frame.storage_remaining_percent,
        )

    def _accept_sequence(self, sequence: int) -> bool:
        if self._last_sequence is not None:
            if sequence == self._last_sequence:
                self._duplicate_frames += 1
                return False
            expected = (self._last_sequence + 1) % 250
            if sequence != expected:
                self._dropped_frames += (sequence - expected) % 250
        self._last_sequence = sequence
        return True

    def _sample_time_in_window(self, timestamp_s: float) -> bool:
        start = self._record_start_unix_s
        end = self._record_end_unix_s
        return (
            start is not None
            and timestamp_s >= start
            and (end is None or timestamp_s <= end)
        )

    def _host_time_in_window(self, timestamp_s: float) -> bool:
        return self._sample_time_in_window(timestamp_s)

    # ------------------------------------------------------------------
    # Diagnostics and output
    # ------------------------------------------------------------------

    def _heartbeat_stats(self, elapsed: float) -> str:
        del elapsed
        return (
            f"frames={self._data_frames_saved} "
            f"pressure={len(self._buf.get('pressure_raw', []))} "
            f"ppg={len(self._arr_buf.get('ppg_raw', []))} "
            f"imu={len(self._arr_buf.get('accel_raw', []))} "
            f"drop={self._dropped_frames} bad={self._bad_frame_count()}"
        )

    def _bad_frame_count(self) -> int:
        return (
            self._decoder.invalid_frames
            + self._parse_errors
            + self._invalid_timestamps
            + self._queue_dropped_chunks
        )

    def _build_output(self) -> dict[str, np.ndarray]:
        out = super()._build_output()

        dtype_by_key = {
            "frame_timestamp_unix_ms": np.uint64,
            "frame_sequence": np.uint8,
            "pressure_raw": np.int32,
            "ppg_raw": np.int32,
            "accel_raw": np.int16,
            "gyro_raw": np.int16,
            "temperature_raw": np.uint16,
            "spo2_raw": np.uint8,
            "battery_raw": np.uint8,
            "storage_remaining_raw": np.uint8,
        }
        for key, dtype in dtype_by_key.items():
            if key in out:
                out[key] = np.asarray(out[key], dtype=dtype)

        out.update(
            {
                "protocol_version": np.asarray("1.5"),
                "device_name": np.asarray(self._selected_device_name),
                "device_address": np.asarray(self._selected_device_address),
                "timestamp_source": np.asarray("device_unix_utc_ms"),
                "ppg_channel_names": np.asarray(["green", "red", "infrared"]),
                "imu_axis_names": np.asarray(["x", "y", "z"]),
                "pressure_sample_rate_hz": np.asarray(150.0),
                "ppg_sample_rate_hz": np.asarray(100.0),
                "imu_sample_rate_hz": np.asarray(50.0),
                "temperature_update_rate_hz": np.asarray(1.0),
                "spo2_update_rate_hz": np.asarray(1.0),
                "accel_scale_m_s2_per_lsb": np.asarray(ACCEL_SCALE_M_S2),
                "gyro_scale_rad_s_per_lsb": np.asarray(GYRO_SCALE_RAD_S),
                "time_sync_sent_unix_s": np.asarray(
                    np.nan
                    if self._time_sync_sent_unix_s is None
                    else self._time_sync_sent_unix_s
                ),
                "time_sync_ack_unix_s": np.asarray(
                    np.nan
                    if self._time_sync_ack_unix_s is None
                    else self._time_sync_ack_unix_s
                ),
                "time_sync_command": np.frombuffer(
                    self._time_sync_command or b"", dtype=np.uint8
                ).copy(),
                "data_frames_seen": np.asarray(self._data_frames_seen, dtype=np.int64),
                "data_frames_saved": np.asarray(
                    self._data_frames_saved, dtype=np.int64
                ),
                "status_frames_saved": np.asarray(
                    self._status_frames_saved, dtype=np.int64
                ),
                "dropped_frames": np.asarray(self._dropped_frames, dtype=np.int64),
                "duplicate_frames": np.asarray(self._duplicate_frames, dtype=np.int64),
                "invalid_timestamps": np.asarray(
                    self._invalid_timestamps, dtype=np.int64
                ),
                "invalid_frames": np.asarray(
                    self._decoder.invalid_frames, dtype=np.int64
                ),
                "discarded_stream_bytes": np.asarray(
                    self._decoder.discarded_bytes, dtype=np.int64
                ),
                "parse_errors": np.asarray(self._parse_errors, dtype=np.int64),
                "queue_dropped_chunks": np.asarray(
                    self._queue_dropped_chunks, dtype=np.int64
                ),
            }
        )
        return out

    # ------------------------------------------------------------------
    # Reset/failure helpers
    # ------------------------------------------------------------------

    def _reset_for_open(self) -> None:
        self._rx_queue: queue.Queue[tuple[float, bytes]] = queue.Queue(
            maxsize=max(0, self.config.queue_max_chunks)
        )
        self._worker_stop.clear()
        self._worker_finished.clear()
        self._connected.clear()
        self._unexpected_disconnect.clear()
        self._worker_error = ""
        self._time_sync_ack.clear()
        self._time_sync_failure = ""
        self._time_sync_command = None
        self._time_sync_sent_unix_s = None
        self._time_sync_sent_monotonic = None
        self._time_sync_ack_unix_s = None
        self._decoder.reset()

    def _fail_open(self, reason: str) -> bool:
        self._open_error = reason
        self._log(f"[wristband] open failed — {reason}")
        self._worker_stop.set()
        thread = self._worker_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        return False
