# 已知故障与定位边界

## 2026-09-23 的正式采集

检查了 systemd 日志及 `data/session-night/2026-09-23-19-20-01/qc_report.json`：

- 左臂 EMG 有 3 个超过 0.100 秒的缺口，累计约 0.33 秒，最长约 0.11 秒。
- 右臂 EMG 有 2 个超过 0.100 秒的缺口，累计约 0.33 秒，最长约 0.22 秒。
- 原框架 QC 因此给出 ERROR，记录未标记 used。原始串口块和报告保留。
- 序号缺口表明接收数据存在不连续，不能仅凭日志区分无线丢包、接线接触或系统调度问题。遵循原 troubleshooting：拔插 USB 适配器、按紧臂环接口；随后重新扫描和试采。不放宽 QC 阈值掩盖问题。
- 19:18:39 出现 MINI_KEYBOARD 断连，服务在约 10 秒后重启并恢复连接。先前两条记录为 cancelled。
- 这些错误没有证据指向阿里云鉴权失败。本次迁移后云语音已再次成功合成。

历史试验中还出现过相机 MPP 流错误，以及用户确认没有手动断电的整机重启。现有证据不足以认定电源、驱动或 stim 为唯一原因。当前程序不启动 stim/pygame，但不能据此保证硬件永不重启。旧诊断证据在私有交付备份中。

## 定位入口

- `sudo journalctl -u embodied-pi-collect.service -b -n 150 --no-pager`
- `orangepi/runtime/controller.json`：状态和当前数据目录。
- `orangepi/runtime/devices.json`：最近一次有效数据扫描。
- `orangepi/runtime/speech_status.json`：云语音最近连接结果，不含密钥。
- 当前会话 `worker.log`、`embodied_recorder.log`、`health_events.jsonl`、`capture_summary.json`、`qc_report.json`。
- `sudo journalctl -b -1 -k`：上次启动内核日志，只有系统持久化日志可用时才能查到。
- `df -h`：Pi 当前空间有限；正式采集前转存历史数据并校验后再清理。程序低于 512 MiB 会停止。

需要复测数据缺口时，先停止正式服务，使用 README 的 smoke 命令。试验数据在 `orangepi/tests/results`，不更新 used。不要同时运行两个采集器抢占串口。

## 2026-09-24 新错误检查

- 14:37 会话：cam_ros_left ROS 源时间戳非递增。
- 15:15、15:22 会话成功；原后处理/QC 分别约 250.75 / 347.97 秒。
- 16:14 会话：cam_ros_bleft MPP 管线 internal data stream error，后续两路 MP4 封装失败。不是凭空推定为 stim 或 API 问题。
- 16:29 会话：board_imu 源时间戳非递增。
- 16:30 会话：录制约 120 秒后系统盘剩余低于 512 MiB，程序保护性停止；检查时根分区仅约 505 MiB。已删除可重新生成的 1.3 GB 交付压缩包，新采集/交付包转至 SD，原数据未删除。
- 对比这些会话主机 wall 与 monotonic 持续时间仅微秒级差异，没有主机整个采集窗口发生大幅时钟跳变的证据。现有错误未保存坏帧源时刻，无法确定驱动/发布端根因；新日志记录前后源时刻、序号和到达时刻，继续严格停止坏数据。
- 17:08 新测试预检中的 EMG 路径消失，用户已确认当时手动调整 USB 连接；不计作自行断连的软件故障。程序不会悄悄丢弃已经确认的设备。

相机时间戳与 MPP 的间歇问题尚不能声称全部修复；遇到相应错误应保留会话日志/摘要，必要时在停止采集后检查/重启厂商 ROS 驱动服务。不要在正式采集时重启驱动。
