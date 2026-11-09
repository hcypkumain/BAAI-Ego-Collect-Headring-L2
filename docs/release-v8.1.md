# Head ring v8.1 — cp_0019

Version: `headring-integrated-20261109-v8.1`. This update is based on the v8.0 source and is deployed on cp_0019. The original cp_0018 v8.0 package and manifest remain unchanged.

A STOP request now closes the sample gate first, then waits for the native recorder to exit and for explicit successful component shutdown and a final summary. Only the completion receipt, tied to the original session and worker, can release the privacy screen. If shutdown is proven but acquisition failed, the result is an error, not a successful save. Missing shutdown evidence keeps the screen covered. Duplicate or delayed requests cannot stop a different session.

Browser START, display registration and black-screen acknowledgement require a client with the session STOP protocol. Previously opened pages must be refreshed; an old page is told to refresh before it can start. Local hardware controls remain available. Idle display rendering preserves the trusted snapshot identity, and an initial connection no longer claims a reconnection before a previous status exists.

The regular installer now uses the updated USB phone and data-host helpers. Unknown ADB enumeration is reported as unknown and retried without automatically restarting the shared server. USB task volumes are accepted only when the visible filesystem is verified read-only with the required mount options; the normal systemd automount scaffold is supported. No existing writable volume is remounted or unmounted.

## Verification and limits

On cp_0019, three isolated TEST recordings used six cameras and the board IMU. The latest exercised the new browser protocol, black-screen acknowledgement, recording-time reload, a real three-second long press and completion receipt. An actual older page was rejected before starting and showed the refresh message. The first test's six video files decoded fully without decoder errors; their counts were 329–332 frames per head-ring camera and 225–226 per wrist camera, with 2,256 IMU rows. Checked arrival timestamps fell between that session's start and stop boundaries. The test sessions were saved in a separate TEST directory with synthetic images and remain pending host QC. This is not a full hardware or quality certification.

No EMG receivers or EEG hardware were attached for these tests. Kernel MIPI CRC/frame-sequence events were observed; the short decodable recordings do not prove the physical camera link is reliable. The earlier cp_0018 phone USB disconnect root cause remains unconfirmed. A stuck dedicated ADB daemon is reported but is not automatically killed, because the existing capture-start and server-recovery paths do not yet share a proven lock. A mode-specific stale token after token rotation is also not automatically repaired. Previously opened phone pages should be refreshed before use.

## cp_0019 package and rollback

`deploy/headring-v8.1-cp0019` contains the exact 10 application/helper files, the device release receipt and their hashes. It is restricted to cp_0019 and to the documented pre-update hashes. The installed copy and consolidated original backup are at `/home/orangepi/TEST-headring-audit-20261109/headring-v8.1-cp0019`. When capture is idle, `python3 install.py verify` checks all 11 deployed and backup hashes. `sudo python3 install.py rollback` restores the actual pre-v8.1 files and receipt and restarts only services that were active. It does not remove recordings, calibration, tasks or configuration; camd is not restarted. Do not use this device-specific package on another head ring.
