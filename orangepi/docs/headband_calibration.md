# 本地 ROS 头环标定文件快照

本功能只增加保存、校验和打包所需的软件能力，不重新标定相机或 IMU，也不修改标定参数、视频、肌电、脑电或任何采样时戳。

配置示例：

```yaml
headband_calibration:
  enabled: true
  source_dir: /opt/egorecord/calibrations
  source_verified: false
  device_id_file: /opt/egorecord/DEVICE_ID
  hardware_model_file: /opt/egorecord/HARDWARE_MODEL
```

同时为 `cameras` 中四个头相机配置实际 `width`、`height` 和 ROS `topic`。校验按话题匹配，允许 Kalibr 原始图像话题和同一话题的 `/compressed` 传输尾缀对应；原文与实际配置均记录，不改 YAML。不按字典列出顺序猜相机位置：本机候选文件按话题对应为 `cam0` 前左、`cam1` 前右、`cam2` 后右 `bright`、`cam3` 后左 `bleft`；更换文件后仍以话题匹配结果为准。

`snapshot(session_root, full_devices_config)` 在开硬件前调用，返回可直接写入本段 metadata/summary 的字典，并原子写 `ego_headband/calibration_status.json`。它完整读取三份非空且不超过 1 MiB 的常规文件，拒绝源目录或文件符号链接，前后比较文件身份、大小、修改信息及 SHA-256，并在原子发布前再次检查源未变化。设备编号和硬件型号文件也须稳定、可读。

只有 `source_verified` 是严格的布尔值 `true`，设备身份可读，两个 camchain 中四相机的话题和尺寸都与本次配置匹配时，才将完整原始文件集发布至 `ego_headband/calibrations/`。`true` 表示维护者已确认文件确属此本体及成像坐标；程序不能代替这种来源认证。

未确认来源或尺寸/话题不匹配时，将完整原件保存至 `ego_headband/calibration_candidates/`，状态为 `candidate`、`applicability_verified=false`，保留明确原因；上游打包器不会从这个候选路径读取未认证参数。语法损坏、缺失、空文件、超限、源变化或保存失败时状态为 `not_available`，不发布部分集合。两类普通警告均允许原有采集继续。

三份原始文件为 `camera-camchain.yaml`、`camera_imu-camchain-imucam.yaml`、`camera_imu-imu.yaml`。快照中的 `manifest.json` 严格采用上游格式 `{"version":1,"files":[{"name":"…","size":123,"sha256":"…"},…]}`，顺序固定如上；没有改写 YAML。目录经完整校验并刷盘后一次重命名发布。每个会话的快照冻结：相同原件可重复核验，不同原件、身份/来源变化、另一认证模式或已损坏的既有快照不覆盖，返回 `conflict=true`，调用者应拒绝重用该会话开录。

主要返回字段：

| 字段 | 含义 |
|---|---|
| `state` | `verified` / `candidate` / `not_available` / `disabled` |
| `available` | 是否有完整已保存快照 |
| `applicability_verified` | 是否允许作为本体匹配的标定使用 |
| `conflict` | 同会话已有不同/另一模式/损坏快照，需要拒绝复用 |
| `reasons`, `warnings`, `message` | 原因代码、警告和中文说明 |
| `device` | 身份值、读取路径及身份文件 SHA-256 |
| `files` | 三个文件的名称、字节数和 SHA-256 |
| `manifest_sha256` | 保存的清单原字节 SHA-256 |
| `source_dir`, `snapshot_relative_dir` | 来源和本段相对目录 |
| `cameras` | 两个 camchain 逐相机的话题、实际/期望尺寸及对应方位 |

准备后、正式 START 前调用 `verify_saved(session_root, status)`，检查三文件及 manifest 原字节 SHA、清单和目录完整性。任一保存后篡改、删除或符号链接会抛 `SnapshotError`，调用者应阻止本段 START。对没有完整快照的 `not_available` / `disabled` 状态，该函数不执行文件检查；候选集也检查字节完整性，但不会因此获得适用性认证。

上游 `scripts/pack_daily.py` 从 `<session>/ego_headband/calibrations/*.yaml` 解析参数，写入数据集 `meta/calibrations.jsonl` 的 `calibrations.ego_headband`，缺失则为 `null`；其加载器不重新校验 manifest，因此本地冻结和 START 前校验仍是必要证据。历史段没有当时快照时不回填为已认证。

2026-10-19 本机只读核查：设备 `cp_0013 / baai-wuyu-ego4-0151-152`，当前四路图像为 1920×1200；安装目录三份候选文件与发布包原件相同，其中相机尺寸为 1920×1088，且文件没有本体身份。因此保持 `source_verified: false`。程序更新完成不代表这些候选数值已经适用于本体；以后取得专用结果并确认尺寸/裁剪坐标后才由维护者明确认证来源。
