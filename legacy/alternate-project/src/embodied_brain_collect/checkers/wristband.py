"""Wristband checker — BLE physiological band: pressure/PPG/IMU + status.

The device UTC clock (synced at open via the 0x20 command) is the
scientific time source, so clock checks run on the device timestamps and
the UTC-sync confirmation is reported as its own check.  ``data_frames_seen``
counts frames outside the recording window too, so frame loss is read from
the device's own sequence-gap counter (``dropped_frames``), not from
seen-vs-saved.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .base import BaseCheck, BaseChecker, CheckContext, CheckOutput
from .checks import DeadChannel, ValueBounds, ts_checks


@dataclass(frozen=True)
class LossCounters(BaseCheck):
    """Protocol integrity counters the recorder ships in the NPZ.

    Any nonzero ``parse_errors`` / ``invalid_timestamps`` /
    ``queue_dropped_chunks`` means data was corrupted or discarded before it
    reached the buffers; ``dropped_frames`` is the device-side sequence-gap
    count and is judged as a fraction of saved frames.
    """

    warn_frac: float = 0.001

    def applies(self, ctx: CheckContext) -> bool:
        return ctx.arr("data_frames_saved") is not None

    def run(self, ctx: CheckContext) -> CheckOutput:
        out = CheckOutput()

        def scalar(key: str) -> float:
            a = ctx.arr(key)
            return float(np.asarray(a).ravel()[0]) if a is not None else 0.0

        saved = scalar("data_frames_saved")
        dropped = scalar("dropped_frames")
        out.stats.update({
            "data_frames_saved": saved,
            "dropped_frames": dropped,
            "duplicate_frames": scalar("duplicate_frames"),
            "parse_errors": scalar("parse_errors"),
            "invalid_timestamps": scalar("invalid_timestamps"),
            "queue_dropped_chunks": scalar("queue_dropped_chunks"),
        })

        frac = dropped / saved if saved else 0.0
        out.stats["dropped_fraction"] = frac
        if frac > self.warn_frac:
            out.findings.append(self.finding(
                "WARN",
                f"设备序号显示丢失 {dropped:.0f} 帧({frac:.2%})",
                field="frame_sequence", threshold=self.warn_frac,
                observed=frac))

        integrity = {k: out.stats[k] for k in
                     ("parse_errors", "invalid_timestamps",
                      "queue_dropped_chunks") if out.stats[k] > 0}
        if integrity:
            detail = "  ".join(f"{k}={v:.0f}" for k, v in integrity.items())
            out.findings.append(self.finding(
                "WARN", f"协议/缓冲完整性计数非零 — {detail}",
                field="raw_packets", observed=sum(integrity.values())))
        return out


@dataclass(frozen=True)
class UtcSync(BaseCheck):
    """Whether the device-clock UTC sync was acknowledged at open.

    Device UTC is the scientific time source; without a confirmed sync its
    offset against the other modalities is unverified.  Dummy sessions run
    on the host clock and skip this.
    """

    def applies(self, ctx: CheckContext) -> bool:
        if ctx.arr("time_sync_ack_unix_s") is None:
            return False
        name = ctx.arr("device_name")
        return name is None or "dummy" not in str(np.asarray(name).ravel()[0])

    def run(self, ctx: CheckContext) -> CheckOutput:
        out = CheckOutput()
        ack = np.asarray(ctx.arr("time_sync_ack_unix_s")).ravel()[0]
        synced = bool(np.isfinite(float(ack)))
        out.stats["utc_sync_acknowledged"] = synced
        if not synced:
            out.findings.append(self.finding(
                "WARN", "设备 UTC 同步未获确认 — 时间戳与主机时钟的对应未经校准",
                field="time_sync_ack_unix_s"))
        return out


@dataclass(frozen=True)
class BatteryLevel(BaseCheck):
    """Last reported battery percentage — a band that died mid-session
    explains a short recording."""

    warn_below: float = 20.0

    def applies(self, ctx: CheckContext) -> bool:
        a = ctx.arr("battery_percent")
        return a is not None and np.isfinite(np.asarray(a, dtype=np.float64)).any()

    def run(self, ctx: CheckContext) -> CheckOutput:
        out = CheckOutput()
        a = np.asarray(ctx.arr("battery_percent"), dtype=np.float64)
        last = float(a[np.isfinite(a)][-1])
        out.stats["battery_percent_last"] = last
        if last < self.warn_below:
            out.findings.append(self.finding(
                "WARN", f"电量仅 {last:.0f}% — 不足以支撑完整会话",
                field="battery_percent", threshold=self.warn_below,
                observed=last))
        return out


class WristbandChecker(BaseChecker):
    """BLE wristband: four device-clocked streams + status at 1 Hz."""

    name = "wristband"
    matches = ("wristband",)
    default_series = "pressure"

    checks = [
        ts_checks("frame", expected_rate=50.0),
        ts_checks("pressure", expected_rate=150.0),
        ts_checks("ppg", expected_rate=100.0),
        ts_checks("imu", expected_rate=50.0),
        LossCounters(),
        UtcSync(),
        BatteryLevel(),
        DeadChannel("ppg_raw"),
        DeadChannel("accel_m_s2"),
        DeadChannel("gyro_rad_s"),
        # 0 = 设备的"本次无读数"(无效样本标记),不是量程越界
        ValueBounds("spo2_percent", lo=0.0, hi=100.0),
        ValueBounds("temperature_c", lo=0.0, hi=45.0),
    ]

    def prepare(self, ctx: CheckContext) -> None:
        ctx.add_series("frame", key="frame_timestamps")
        ctx.add_series("pressure", key="pressure_timestamps")
        ctx.add_series("ppg", key="ppg_timestamps")
        ctx.add_series("imu", key="imu_timestamps")
        ctx.add_series("status", key="status_host_timestamps")
