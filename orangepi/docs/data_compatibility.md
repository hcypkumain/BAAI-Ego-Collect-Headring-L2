# 数据接口对照（orangepi 整理版）

原 `src/embodied_brain_collect` 未修改。采集使用 BaseRecorder 复合适配器，后处理调用原 EMG rebuild、元数据函数、scene_layout、qc_session 与 checkers。不是逐字节或所有事件等价。

| 文件/目录 | 原项目已有的约定 | 当前与新增内容 |
|---|---|---|
| `<SD>/Embodied-Brain-Collect/<device_id>/data/session-night/YYYY-MM-DD-HH-MM-SS` | 原 data/班次/时间会话名 | 内部布局一致，新增 SD 外层和设备编号；storage.yaml 配置，禁止回落系统盘 |
| `meta.yaml` | version/framework、environment、recorders、scene/objects | 调用原元数据函数；environment 相对于 configs/environments；增加起止时刻、duration、reference_sha256、pi 审计、status 等 |
| `emg_left/emg_left.npz`、`emg_right/emg_right.npz` | 原 EMG 槽位、文件名、信号字段 | 原字段保留，另增原始帧与单调时间信息 |
| `cam_ros_*/cam_ros_*.npz` + `frames.mp4` | 原腕部相机 `<slot>/<slot>.npz` 与 frames.mp4 | 遵循相同结构；真实槽位是 ROS 四相机，不伪装成 cam_left_wrist/right_wrist；硬件 HEVC 而非保证原设备同编码 |
| `cam_left_wrist/cam_left_wrist.npz`、`cam_right_wrist/cam_right_wrist.npz` + 各自 `frames.mp4` | 原 USB 腕部槽位和文件名 | USB 3.0，640×480、30 Hz、MJPG 输入、MPP HEVC 输出；沿用原 30 Hz CameraChecker |
| `marker/marker.npz` | 原 marker 槽位、事件代码 | 只记录实际软件开始/停止事件；见下文限制 |
| `board_imu/board_imu.npz` | 原项目没有此槽位 | 新增独立板载 IMU，不混入臂环 IMU |
| `qc_report.json`、可选 `qc.html` | 原报告结构与阈值 | 仅适配相机 20 Hz、board IMU 200 Hz；ERROR 不记 used |
| `<slot>/<slot>.log` | 原 recorder 也有槽位日志 | 当前为实际硬件采集摘要；逐行文本不等同于原实现；复合适配器另有 embodied_recorder.log |
| 图库各目录 `used.yaml` | used 映射的 session/at | 成功沿用；failed 是新增追踪段，不影响原程序读取 used |

## EMG NPZ

只为启动时实际接入并确认的 EMG 生成对应目录，两臂的字段结构相同。原有字段：`emg_data` int32[N,8]；`emg_sn` int64[N]；`emg_timestamps`、`emg_arrival_timestamps` float64[N]（Unix 秒）；`imu_sn` int64[M]；`imu_timestamps`、`imu_arrival_timestamps` float64[M]；`imu_gyro`、`imu_accel` float32[M,3]。24 bit 有符号 EMG 解析、gyro×0.0012、accel×0.0005978 与原设备实现一致，不擅自把 EMG 原始数值标成微伏。

新增：`emg_monotonic_ns`、`imu_monotonic_ns` int64；`raw_frames` uint8[K,29]（包含 EMG 与 IMU 帧）；`raw_arrival_unix_ns` int64[K]；`timestamp_basis` 字符串。`timestamp_rebuild.json` 记录原算法拟合是否接受及误差。重建不接受时保留到达时间并明确说明，不伪造平滑时间。

`raw_chunks/chunk_XXXXXX.npz` 保留原始分块。分块序号 dtype 是 uint8，合并标准文件转换为原接口 int64。只有顶层一个标准 NPZ，避免原 checker 误选分块。

## 相机 NPZ 与视频

原标准字段 `frames_timestamps` float64[N]。ROS 相机使用源 header.stamp，新增 `ros_stamp_ns`、`arrival_unix_ns`、`arrival_monotonic_ns`、`video_pts_ns`、`ros_sequence` 都为 int64[N]。USB 腕部沿用原 USB 实现的主机收帧 Unix 秒，新增 `arrival_unix_ns`、`arrival_monotonic_ns`、`video_pts_ns`、`source_pts_ns` 为 int64[N]，另有 timestamp_basis 字符串。USB 不写伪造的 ROS 字段。**两类相机时间来源不同，不代表硬件同步。**

`timestamps.csv` 对应 frame_index 与上述纳秒字段；`video_check.json` 为 ffprobe 包数、编码、尺寸等审计。MP4 为 MPP JPEG 解码 → NV12 → MPP H.265 编码；ROS 为 1920×1200、约 20 Hz，腕部为 640×480、30 Hz。停止后封装截止前已接受的帧，不接收确认期间的新帧。USB 同时拒绝在 START 前捕获但延迟送达的缓存。原 QC 仍会检查帧数、黑帧和冻结。

当前只写 frames.mp4。旧 video.mp4/frames.mp4 如为同 inode 则是同内容硬链接，不应拼接。历史文件原样保留。

## 板载 IMU NPZ

`timestamps` float64[N]；`ros_stamp_ns`、`arrival_unix_ns`、`arrival_monotonic_ns`、`sequence` int64[N]；`orientation` float64[N,4]（xyzw）；`angular_velocity`、`linear_acceleration` float64[N,3]（rad/s、m/s²）；各自 `*_covariance` float64[N,9]；`orientation_valid` bool[N]。另保留 raw.csv。

当前实际消息 orientation 全零，不是有效姿态，因此 validity=false；角速度、加速度有数据。没有算法姿态、磁力计数据，不添加不存在的测量。

## marker 评估

RUN_START=241、RUN_END=242 来自原 marker 配置；`marker_code` int64[2]，`marker_tag` 字符串[2]，`marker_t_sent_pc` 和 `marker_t_local_recv` float64[2]。

倒计时结束开门控生成 START，**录制中第一次 A 关闭门控时生成 END**，不是第二次确认时刻。致命停止的 END 同样是实际关闭门控时刻。新增 source 明确是主机软件门控，没有 TTL。两个时间字段相等，不代表测量过 UDP/同步盒延迟。

没有运行 stim、E-Prime、UDP 发码或同步盒，因此缺少原 UDP recorder 可有的 `marker_trial`、`marker_t_eprime_ms`，没有 FIX/IMG/EXEC 等刺激事件。不制造这类假数据。原 QC 可以读这些 START/END 确定窗口；这不构成与 EEG 或其他设备的硬件同步。

## 其他审计与限制

- `meta.json`：参考图片、SHA256、设备配置、test/stim/pygame 标记和时间说明。
- `stop_gate.json`：首次关闭门控的 wall/monotonic 纳秒时刻。
- `status.json`：工作阶段、实时活跃状态（采集阶段）、计数/警告；QC 阶段由框架接管。
- `native_capture_summary.json`：原生成功采集摘要快照；`capture_summary.json`：最终采集/QC 结论、设备计数、编码和 qc_elapsed_seconds。
- `health_events.jsonl`：有非致命数据连续性警告时才出现；`retry.json`：人工 B 重采；`controller_error.json`：保存超时等控制错误。
- `worker.log`、`embodied_recorder.log`：进程和原接口日志。
- 致命错误可能使标准化无法完成；此时保留原始证据，不保证每个标准文件均可生成。
- 不启用 pack_episode；原每日 mf_lerobot 转换器没有 board_imu 特征映射。完整原始数据打包使用 orangepi/scripts/storage/package_data.py，保留板载 IMU。未连接的 EEG、眼动、手套、定位、腕带无对应数据；腕部 USB 已接入，仍只为启动时确认的设备生成目录。
- 旧 addon/data 已整体搬迁至 data/orangepi-legacy；used.yaml 引用随之修正，内部历史元数据内容未改，映射记录在私有备份。其他原 data/session-night 会话未移动。

## v0.6 存储与选择补充

`device_selection.json` 保存启动时确认的槽位列表，本次程序所有记录沿用该列表；未确认槽位不创建空目录、不参与 QC。`meta.json` 增加 selected_devices/storage_config，原 meta.yaml.recorders 仅列实际采集槽位。SD 路径不改变 NPZ/MP4/marker 内部格式。试采在 SD diagnostics，正式数据在 SD data/班次。QC 只并行调度独立原 checker，不修改阈值；对照验证报告完全一致。
