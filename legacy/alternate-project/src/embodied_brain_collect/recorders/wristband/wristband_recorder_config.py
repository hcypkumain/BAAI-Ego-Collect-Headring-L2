"""Configuration for the physiological wristband recorder."""

from dataclasses import dataclass

from ..base import BaseRecorderConfig


# Nordic UART Service (NUS) UUIDs supplied by the mobile App engineer.
DEFAULT_SERVICE_UUID = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
DEFAULT_WRITE_UUID = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"
DEFAULT_NOTIFY_UUID = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"


@dataclass
class WristbandRecorderConfig(BaseRecorderConfig):
    """Connection and buffering knobs for one wristband.

    ``address`` is the safest selector when several wristbands are nearby.
    On macOS Bleak exposes a CoreBluetooth identifier rather than a public MAC
    address; copying the identifier printed by the hardware test is supported.
    If neither address nor name is supplied, discovery matches the advertised
    service UUID.
    """

    device_name: str = ""
    address: str = ""
    service_uuid: str = DEFAULT_SERVICE_UUID
    write_uuid: str = DEFAULT_WRITE_UUID
    notify_uuid: str = DEFAULT_NOTIFY_UUID

    scan_timeout: float = 10.0
    connect_timeout: float = 10.0
    sync_timeout: float = 5.0

    # The device transmits the previous second's 50 frames as one burst.  Keep
    # listening briefly after stop so samples captured just before the session
    # boundary can arrive; timestamps after the boundary are discarded.
    tail_wait: float = 1.5

    queue_max_chunks: int = 4096
    store_raw_packets: bool = True

    # None = choose from the characteristic properties automatically.
    write_with_response: bool | None = None
