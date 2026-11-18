# Reliability update 2026-11-19

> Historical notes for release `c4c00f1`; superseded by [common release notes](common-release-20261119.md). The limitations below describe that earlier release.

This source release combines the 13/23 phone reconnection pilot, the 13 device-error and preflight display improvements, the 13 abandon-preparation optimization, and the EEG final-summary ordering correction. Device-specific choices (impedance skipping on specific headbands and 150 ms EMG threshold on 17/18) are not generalized by this release. Existing per-device configuration and data must be preserved when deploying.

The Android app displays a native connection-lost state and probes the read-only workbench endpoint, without replaying capture controls. A signed v5 version 10 package was trialled on 13; an idle USB disconnect/reconnect and a single recording-time disconnect/reconnect were observed. Full long-duration and broader-device testing remains outstanding.

The USB DHCP recovery addresses the legacy unleased phone interface with an interface-only IPv4 reapply, including during recording; it does not reset the USB controller. It remains constrained by the device's existing selector, and does not replace the persistent legacy network configuration. Validate target compatibility and existing phone leases before installation.

Task abandonment avoids the first redundant refresh and computes the completed/reserved/history results in one pass. Pending-QC metadata is parsed once per item; changing metadata during read is rejected. No cross-refresh evidence cache is introduced.

QC persists finalized EEG sample counts before comparing full-stream statistics. Historical QC results are not changed by this release and require separate, evidence-preserving reassessment.

Known limitation: deleting a pending-QC recording may release its ledger reservation without removing that image from task-block saved counters. A one-off repair was performed on 13 for the specific test segment; this release does not provide a general atomic delete/reconcile fix. Do not claim that deleted saved counts are automatically restored. Existing recordings and block history require evidence-based reconciliation.

Android artifact: `services/phone-usb-helper/android/artifacts/headring-helper-v5-v10.apk`; package `org.headring.usbentry.v5`, versionCode 10, SHA-256 `09f164a5034ed0ce709a521e38aefdf36992439c2b146a9baa1a654cc2bccca5`, signing certificate SHA-256 `d84ecf94660f7e1e76cbd9717312462e55c0a7107810700ed030566c6e5845f9`. It is not a replacement for the legacy v4 package. The v10 app was trialled on 13 only.
