# 与原项目的目录及数据接口评估（2026-09-28）

## 评估结论

原项目使用根 `src/embodied_brain_collect`、`configs`、`scripts`、`tests`。
Pi 的 `orangepi/src`、`configs`、`scripts`、`tests` 已采用相同顶层分工；
此前的主要差异在于设备实现集中在 `acquisition/capture.py`，且脚本依靠平铺的
`sys.path` 导入，并不是缺少 `src/configs` 名称。

本次将 ROS/UVC 共用的 MPP 相机实现移入
`src/orangepi_collect/recorders/camera/mpp_camera_recorder.py`，USB 输入和规格验证放在
同目录 `uvc_source.py`，与原项目 `recorders/camera` 的职责对应。
新增驱动使用包导入。`acquisition/capture.py` 继续负责设备生命周期、公共起止门控、
异常汇总；原有 EMG/板载 IMU 实现暂留其中。既有命令入口及兼容导入仍保留，
不把这次接入夸称为所有模块已经完全重构。

| 原项目职责 | Orange Pi 对应位置 | 边界 |
|---|---|---|
| `recorders/camera` | `src/orangepi_collect/recorders/camera` | ROS/UVC 输入、MPP、帧时间戳 |
| `session` | `control/launcher.py`、`acquisition/capture.py` | 键盘流程、设备选择、统一起止 |
| `checkers`、元数据 | `framework/` 调用原框架 | 不复制或放宽原 QC 阈值 |
| `configs` | `orangepi/configs` | 设备、存储、性能、语音、环境锁定 |
| `configs/environments` | 仍使用根目录原图库 | 不复制第二套图片和 used 台账 |
| `scripts` | `orangepi/scripts` | 启动、设备查找、试采、打包、服务安装 |
| `tests` | `orangepi/tests` | 软件回归；实机数据位于 SD diagnostics |
| 运行产物 | `orangepi/runtime`、SD data/diagnostics/delivery | 不混入源码或公开源代码包 |

## 数据兼容性

腕部使用原槽位 `cam_left_wrist` / `cam_right_wrist`，各自保存同名 NPZ 和
`frames.mp4`；NPZ 主字段 `frames_timestamps` 为 float64 Unix 秒，与原 USB
相机的主机收帧时间语义一致。新增到达时间、源 PTS 和视频 PTS 只用于审计，
不伪称 ROS header 或硬件触发时间。MPP 使用 HEVC，文件容器仍为 MP4。

原 `scripts/pack_daily.py` 的 `discover_video_slots` 按 `cam_*/frames.mp4`、
同名 NPZ 和 `frames_timestamps` 自动发现相机，所以腕部命名符合其输入接口。
它默认读取根 `data`，在 SD 场景必须显式指定 `--source`；设备显示名优先读取
会话 `meta.yaml.recorders`，新腕部写入 USB wrist camera，不沿用其旧 USB2.0 默认名。

这不等同于已验证全部 mf_lerobot 转换：原每日转换器没有板载 `board_imu`
特征映射，且依赖另外安装的 mf_lerobot。完整原始会话交付使用新增
`scripts/storage/package_data.py`，保留板载 IMU、原始 EMG、所有视频及诊断文件。
`package_delivery.sh` 只打包程序、配置、图库、台账及语音资源，不包含采集数据。

## 保留的有意差异

- Pi 以额外目录交付，不能只有一套顶层 src：根 src 是复用的原框架，orangepi/src 是平台适配。
  直接把新平台代码塞进原包会增加原项目的 ROS/GI 平台依赖，因此本次不这样合并。
- Pi 的 `configs/environments/{ros,framework}` 是两套 Python 依赖锁，根
  `configs/environments/场景` 是参考图库，作用不同。安装脚本指向前者；图像队列和 used 指向后者。
- 既有控制和存储脚本的平铺导入仍为兼容入口。新驱动采用包路径；后续若整体迁移到
  包入口，应同时更新 systemd、CLI、测试和交付文档，不能只重命名目录。
- 单独的 `runtime`、SD `diagnostics` 和 SD `delivery` 用于隔离日志、试采、交付包，
  不应放回源码或正式会话目录；每次试采使用新时间目录，失败样本也保留。
