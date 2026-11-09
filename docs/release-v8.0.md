# v8.0 — integrated cp_0018 deployment

Version: `headring-integrated-20261109-v8.0`.

The release consolidates USB task mounting, authorized multi-device access and port recovery, startup rendering, failed scan retry, STOP feedback, bounded camera buffering, and saved daily duration. It retains the existing EEG/EMG profile switching. Unverified changes to native per-sample EEG timestamps are excluded.

The installer was applied on cp_0018. Its listed file hashes matched the package; 45 configuration files retained their SHA256 values, and 54 existing recording files retained their paths, sizes, and modification times. The camera daemon retained its process ID. The head ring reached device confirmation, with four head-ring cameras, two wrist cameras, and the board IMU passing discovery. EMG receivers were not attached during that check.

A subsequently attached exFAT USB drive mounted with `ro,nodev,nosuid,noexec`. No task import or disk-content modification was performed during verification.

## Phone disconnect investigation

The phone ending in 0822 experienced kernel-level USB disconnects several seconds after initial attachment, without user unplugging. A controlled comparison temporarily stopped automatic phone handling while leaving the ADB server running. After one replug, the phone stayed online; reverse-port creation, browser opening, and the automatic service were restored in separate steps. The user confirmed the collection page was accessible. The same USB device number remained present for approximately 27 minutes through the 19:00 check.

No unique root cause was established, and no permanent USB reliability fix is claimed. The integrated program was not changed during this comparison. The phone-side netcat HTTP probe did not return a usable response; successful page rendering was confirmed by the user. Extended recording validation and simultaneous multi-phone hardware testing remain outstanding.

## Rollback

Use the rollback helper within the installed package on the head ring, only when the workflow is idle and no recording session is active. It restores the specific installation backup and previous service states. Do not run the cp_0018 installer against other head rings or replace station configuration with this export.
