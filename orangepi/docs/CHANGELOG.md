# 变更记录

## 0.2.0 — 2026-09-22

- 修复 PulseAudio 地址写死导致的启动失败；播放成功后才报告启动完成。
- 新增 --image 指定图像、list-images；成功保存和 QC 后自动退出，只采一张，无需 Esc。
- 新增板载 /imu_data_raw 采集，与 EMG 臂环 IMU 分开保存。
- uv 双环境：系统 ABI 兼容的 ROS 环境 + Python 3.11 原框架环境，提交锁文件和迁移脚本。
- 新增原 BaseRecorder 的复合适配器，复用原时间戳重建和 qc_session；标准相机/EMG/marker/meta 输出，新增板载 IMU Checker。
- 短暂丢帧记录为异常并交由最终 QC 判定；断流、设备断开及写入错误仍终止。失败样本不删除、不记为完成。
- 原框架 QC 在采集结束后运行，单核、低优先级、计算库单线程；状态 JSON 增加 fsync，保留原生采集结论副本。
- 16 项单元/接口测试；25 秒七路测试正常自动退出，原版 QC 为 WARN、无 ERROR。
- 记录一次自动重启和未确定的根因；不将短时通过当作长期稳定证明。
- 语音升级仅提供方案，未更换引擎或启用 NPU。

## 基线

- e062cd0 / pi-before-startup-fix-20260922：原框架和第一版 Pi 入口快照。
- ea9da1f / pi-startup-fixed-20260922：音频启动修复。

## 2026-09-23 v0.3
- Enter 进入；A 播报、B 重播、A 倒计时开始；A 请求结束、A 确认保存退出或 B 保存旧条并重采同图。
- 直接加载原 troubleshooting.py EMG 排错指引，播报 QC 具体问题。
- 新正式数据采用原 data/session-night/日期时间 路径约定，支持 --session-dir；原元数据写入函数生成 environment/recorders 等；补 marker_tag 并对齐整数类型。历史数据不变。
- QC 从单小核改为单大核，nice15 改为 nice5；同一 25 秒会话约 231→75.8 秒，完整报告一致。
- 增加云端语音缓存/超时本地降级评估，未启用外部 API。

## 2026-09-23 v0.4
- 接入官方 DashScope 1.27.6 / qwen3-tts-flash-realtime，独立秘密文件、云缓存、本地降级和后台退避重试。
- 全部原 troubleshooting 类别按相关设备匹配播报，追加会话级/流级 QC 详情，B 可重播错误排查信息。
- 正式采集只用 A/B，保存/QC 后继续下一张，失败等待修复重采；保留终端 --console 调试。
- 统一原 used.yaml，迁移旧成功台账；失败数据保留为 failed/error，并可用 ledger.py failed 重新入池。
- 安装 systemd 开机服务，等待 MINI_KEYBOARD 并允许完整键盘终端停止/重新开始。
- 新视频只生成 frames.mp4，历史硬链接数据保持原样。
- 确认停止优先发出 STOP，再播放提示，避免积压警告延迟结束。

## v0.5.0 — 2026-09-24 delivery organization

- Rename addon to orangepi; classify configs/src/scripts/docs/tests, isolate credentials and virtualenvs.
- Verified private archive before cleanup; retain all formal data, relocate legacy data with ledger path mapping.
- First A closes native acceptance gate before confirmation speech; preserve that boundary for marker END.
- Silence nonfatal speech while recording; report original QC/troubleshooting and raw warnings after stop.
- Startup validates device data; A confirms, B rescans. Selected first reference continues sequentially.
- Rewrite migration/setup/activation/device/API/debug/data documentation; portable systemd installer and source exporter.
- 37 automated checks and a 12-second real four-camera/two-EMG/board-IMU acquisition validated; QC WARN preserved.

## v0.6.0 — 2026-09-24 device selection, cloud-only speech, SD storage

- A confirms any non-empty set of detected acquisition devices once per launch; capture and QC use only those slots.
- Remove local synthesis; Aliyun-rendered safety assets provide wait/error-code/timeout speech offline. Bound synthesis waits and stop with code 78 instead of unattended restart loops.
- Parallelize unchanged original stream checkers with CPU/memory headroom; identical report on real historical data.
- Use validated SD UUID/mount/device-specific paths for all new captures, diagnostics and source exports; never fall back to system disk.
- Preserve existing internal-disk recordings; remove only the reproducible old export after code/config/ledger backups.
- Add explicit preview/apply ledger reset/rebase tools and update migration, storage, naming and package instructions.

## v0.6.1 — SD-hosted source deployment

- Compare the recording volume with the actual root filesystem, so source code may also reside on the data SD card without a false system-disk refusal. Added regression coverage.
## 2026-09-28 USB 腕部相机接入

- 在 Pi 实测复现双 USB 2.0 同总线带宽分配失败，按用户要求使用两台 USB 3.0 Camera3。
- 新增 `recorders/camera` 包，共用 ROS/UVC 的 MPP JPEG 硬解、H.265 硬编和统一起止门控。
- 腕部沿用原 `cam_left_wrist`、`cam_right_wrist` 目录、同名 NPZ、`frames.mp4` 和 30 Hz QC；
  固定 by-path 左右设备映射，添加真实出流扫描、设备列表和隔离试采工具。
- 720p30 全设备试采出现重启后，默认回到原项目 640×480/30 fps；三线程 QC 又出现重启，
  默认改为单工作线程、CPU 7，手动启动补齐 FFmpeg 线程限制。根因未确诊，详见验证记录。
- 明确代码/配置/脚本/测试/运行数据分工；源代码打包改为 orangepi 明确白名单；新增完整原始
  会话打包脚本，保留文件名和内部格式，不假装支持尚未映射的板载 IMU 数据集转换。
- 新增六项腕部门控、规格、数据接口和 QC 分流测试。
