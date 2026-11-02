# Orange Pi 采集交付说明

本说明对应整理后的 `orangepi`，替代旧 `pi_ros_emg_20260922` 使用说明。所有命令默认在 Pi 的项目根目录 `/home/orangepi/Desktop/Embodied-Brain-Collect-main` 执行；换设备后改为实际目录。原项目根 README 保留作为框架资料。

当前支持的数据采集设备：四路 ROS 鱼眼相机、两路 USB 3.0 腕部相机、两套 EMG、板载 IMU；MINI_KEYBOARD 是控制键盘，不计入采集设备清单。肌电先显示 A/B，由操作者观察握拳时的实时信号，在网页人工指定左右并确认；序列号仅标识设备。首次开机或重新插拔后需重新确认，详见 `docs/emg_boot_calibration.md`。六路相机使用 Rockchip MPP JPEG 硬件解码、H.265 硬件编码；不启动 stim，不依赖 pygame，不使用 NPU。完成网页左右确认后可用 A/B 控制采集。腕部规格和设备查找见第 12 节。

头环每段会在开录前归档三份原始标定 YAML 及 SHA-256 清单，开录时再次检查已保存副本。当前安装文件的来源和图像尺寸对应尚待核对，因此默认保存为候选资料并提示；来源确认且匹配的标定才进入标准打包目录。详见 `docs/headband_calibration.md`。此功能不重算参数，不修改原始视频、IMU 或历史记录。

## 1. 采集员操作流程

1. 开机后自动启动采集服务，等待已配对的 MINI_KEYBOARD 接通。程序扫描设备的有效数据，播报已接入设备和缺失设备。
2. **A 确认当前已接入的数据采集设备，B 重新扫描。** 允许部分设备接入；按 A 后锁定本次程序的设备清单，未检测到的设备不创建数据目录、不参与 QC。零设备禁止确认。此交互只在启动后进行一次，B 可在确认前反复重扫，后续各段不再询问。每段仍检查已确认设备是否正常；已确认设备中途掉线会停止该段，不会悄悄缩减设备。若要增减设备，结束程序并重新启动。
3. A 播报第一张参考图像名称；B 重复播报。此时准备设备，但尚未保存正式样本。
4. 再按 A，播报“一、二、三”，倒计时结束才开始记录。**采集中不发音，B 无动作。** 非致命告警写日志，结束后播报；致命错误先停止收数，再播报错误和原项目相关排查提示。
5. **采集中第一次按 A，立即关闭收数门控。** 程序确认门控关闭后播报“采集已停止，按 A 确认保存，按 B 重新采集当前图像”。确认期间不继续采样；截止前已经接收的缓存仍需写盘，视频需封装，QC 可以开始运行。
6. 按 A 保留该条，等待保存、QC 和报错播报。QC 非 ERROR 才标记 used，然后自动按顺序准备、播报下一张；下一张仍需按 A 倒计时开始。按 B 则保留旧条数据、标记 failed/error，待本条 QC 结束后重新准备同一张，绝不覆盖旧文件。
7. QC ERROR 不标记 used，停在当前图像：A 重采，B 重复排查提示。到队列末尾播报完成，A 可重新读取待采集队列。

语音和 QC 阶段请等播报结束再按键，避免误触。设备扫描、准备、倒计时、保存和 QC 中不接受开始下一条。停止整个程序用第 6 节调试办法。退出时未确认的条目不记 used。

### 图像顺序与 used

- 图库为原 `configs/environments/`。只选编号 PNG，排除总览图；顺序为图像相对路径排序（目录、文件名；编号采用原文件的补零顺序）。
- 默认首张是排序最前的未完成图像；failed/error 条目仍属于未完成。确定首张后保留这次队列，向后逐张采集，跳过已有有效 used 的图像；不会中途跳回更早的图像。
- 固定首张：编辑 `orangepi/configs/devices.yaml` 的 `first_image`，例如 `餐桌_倒水_0003.png`；置 `null` 恢复自动选择。名称重复时使用相对于 `configs/environments` 的完整路径。
- 临时指定：停止自动服务后执行 `bash orangepi/scripts/run.sh foreground --image 餐桌_倒水_0003.png`。显式指定的首张即使 used 也允许重新采；之后仍按队列向后走。
- 成功且用户按 A 保留后，自动写图像所在目录的原 `used.yaml`：`used: {图像文件名: {session: 绝对会话目录, at: 时间}}`。无需人工添加 used。
- failed 是新增追踪段，保存最近失败会话；完整历史仍在数据目录中。失败尝试不删除同图像已有的另一条成功记录。QC WARN 会播报并保留报告，仍可成功标记；QC ERROR 不可。
- 事后判定某条无效：先停止程序，执行：

```bash
source orangepi/scripts/env.sh
orangepi/.venvs/ros/bin/python orangepi/src/orangepi_collect/storage/ledger.py failed \
  --session /完整路径/data/session-night/2026-09-23-19-20-01
```

此操作改 meta.status 和 used/failed，不删数据。台账修改前自动备份到 `orangepi/runtime/ledger_backups/`。下一次启动会纳入待采集队列；已经运行的队列不会悄悄重排。全新实验需要新的台账，请先备份再手动将相应 used.yaml 改为 `used: {}`，不要连同旧实验台账直接清空原件。

## 2. 目录和需要复制的文件

```text
Embodied-Brain-Collect-main/
  src/embodied_brain_collect/        原框架：接口、时间重建、QC、元数据、排查提示
  configs/                          原配置、markers/checker、参考图像、逐场景 used.yaml
  scripts/ tests/                    原项目工具和测试（保留）
  pyproject.toml requirements.txt    原框架信息（保留）
  orangepi/
    README.md
    configs/
      devices.yaml                  设备 ID、话题、首张图像
      storage.yaml                  SD 卡 UUID、挂载点、设备编号、班次目录
      performance.yaml              QC 核心、并发、内存限额
      speech.yaml                   云语音模型、声音、超时、重试策略
      secrets.env.example           空凭据示例
      environments/{ros,framework}/ 各自 pyproject.toml + uv.lock
      systemd/                      可移植开机服务模板
      driver_reference/             本机驱动启动/设备映射快照，仅供核对
    src/orangepi_collect/
      control/                      A/B 状态机和语音调度
      acquisition/                  ROS/串口采集、有效数据扫描
      recorders/camera/             ROS/UVC 相机、公共 MPP 保存、USB 输入驱动
      framework/                    BaseRecorder 适配、标准输出与原 QC
      storage/                      used 台账
      audio/                        阿里云 SDK、缓存、超时保护和重连
      common/                       路径及配置入口
    scripts/
      run.sh env.sh setup_envs.sh check.sh package_delivery.sh
      service/                      安装/更新开机服务
      audio/                        云语音测试、预热、安全提示音生成
      storage/                      台账预览、重置、路径更新、原始会话打包
      diagnostics/                  手动故障、性能和系统监测工具
    assets/cloud_voice/             阿里云预合成的安全提示音与校验清单，必须复制
    tests/                          自动化回归测试；新硬件试采在 SD 卡 diagnostics
    docs/                           数据对照、诊断、依赖版本和变更记录
    private/secrets.env             本机密钥，禁止提交 Git 或放进公开交付包
    .venvs/{ros,framework}/         本机环境，换设备重建，不复制
    runtime/                        状态、日志、云语音缓存、台账备份，非源代码
  data/session-night/时间目录/       系统盘已有历史数据（新采集已改到 SD 卡）
  data/orangepi-legacy/              从旧 addon/data 保留搬迁的历史正式数据
  delivery_backups/                  本次整理前私有完整备份，不放公开交付包
```

**不能只复制 orangepi！** 必须同时复制原 `src`、完整 `configs`（包括参考图片和需要续采的 used.yaml）、根项目文件。推荐使用白名单打包：

```bash
bash orangepi/scripts/package_delivery.sh
```

默认输出到配置的 SD 卡 `Embodied-Brain-Collect/orangepi-01/delivery/`。可用 `bash orangepi/scripts/package_delivery.sh /另一块磁盘/交付目录` 指定位置。包含原框架源码、orangepi 源码、全部图库、**当前 used.yaml**、配置、文档、测试、uv.lock 和阿里云安全提示音，附 SHA256。不会包含密钥、原始采集数据、历史备份、Git、虚拟环境和运行缓存。打包前核对目标空间，至少保留 1 GiB 余量；未完成文件用 .partial，不可当交付包使用。建议停止采集后打包，确保台账一致。将 tar.gz 和校验文件复制到新设备，在目标项目目录执行：

```bash
sha256sum -c orangepi-source-具体时间.tar.gz.sha256
tar -xzf /来源/orangepi-source-具体时间.tar.gz -C /目标/Embodied-Brain-Collect-main
```

数据需要单独复制并校验。继续旧实验时带上 used.yaml；若改变数据所在绝对路径，核对台账中的 session 路径并按实际迁移位置修正，先备份台账。只迁源代码且不迁历史数据时，旧 used 仍保留跳过语义，但旧报告不能在新机本地打开。

若需要完整提交历史，另运行 `git bundle create /media/orangepi/sdcard/Embodied-Brain-Collect/orangepi-01/delivery/source.bundle --all`；新机可 `git clone /来源/source.bundle Embodied-Brain-Collect-main`，再单独复制完整 configs/environments 图库、所需运行台账和密钥（它们不在 Git 历史中）。打包脚本复制当前文件；bundle 保存已提交版本，两者不能混为一谈。

## 3. 新设备安装环境

### 系统部分（uv 不能替代）

当前验证平台是 RK3588、Ubuntu 20.04 arm64、ROS Noetic、系统 CPython 3.8。详情见 `docs/SYSTEM_DEPENDENCIES.txt` 和 `docs/DELIVERY_SYSTEM_PACKAGES.txt`。优先使用相同设备厂商镜像；普通 Ubuntu 或其他开发板不能保证具备摄像头驱动及 MPP。

需要厂商 `ros-noetic-seeker-sensors`、`ros-noetic-seeker-cam-bridge`、Rockchip `gstreamer1.0-rockchip1`、ROS Noetic、对应内核/设备树/固件。它们安装在系统 `/opt/ros/noetic` 等目录，**没有被源代码交付包包含**，需在新机使用厂商镜像/安装包安装。`configs/driver_reference` 只是配置快照，不是驱动安装包。

在已配置对应系统软件源的相同镜像上，基础依赖可安装：

```bash
sudo apt update
sudo apt install python3-gi python3-numpy python3-yaml gir1.2-gstreamer-1.0 \
  gstreamer1.0-tools gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
  ffmpeg pulseaudio-utils bluetooth v4l-utils usbutils
# ROS、seeker 和 Rockchip 插件按厂商安装说明配置；检查实际可用：
source /opt/ros/noetic/setup.bash
rospack find seeker_sensors
rospack find seeker_cam_bridge
gst-inspect-1.0 mppjpegdec
gst-inspect-1.0 mpph265enc
```

新机还需安装并启动 `seeker_imu.service`、`seeker_cam_bridge.service`；模板在 driver_reference，安装前核对驱动路径和物理映射。新机 `/dev/video*` 很可能不同。不要直接把旧映射覆盖到新机。安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)（`curl -LsSf https://astral.sh/uv/install.sh | sh`，然后 `source "$HOME/.local/bin/env"`），确保当前用户的 `uv` 在 PATH 中，然后：

```bash
bash orangepi/scripts/setup_envs.sh
bash orangepi/scripts/check.sh
```

setup 使用已锁定的 uv.lock。ROS 环境通过 `--system-site-packages` 使用系统的 rospy/GI/NumPy/YAML；不能用框架的 Python 3.11 加载系统 3.8 的二进制扩展。框架环境使用 Python 3.11，由 uv 安装固定版本的 NumPy、SciPy、OpenCV、PyYAML、Loguru、DashScope SDK。不是没有额外库，只是启动脚本直接调用环境内的解释器，所以平时无须手工 activate。

### 手工激活与调试

```bash
source orangepi/scripts/env.sh
source orangepi/.venvs/ros/bin/activate
python orangepi/src/orangepi_collect/acquisition/scan_devices.py
deactivate

source orangepi/.venvs/framework/bin/activate
python -c 'import dashscope, numpy; print("framework OK")'
deactivate
```

`run.sh` 自动加载 ROS 环境并选解释器。框架由源目录导入，无需在系统 Python 中安装整套原 requirements.txt。换机器不要复制 `.venvs`，绝对路径和 ABI 会失效。首次 uv 安装依赖需要网络；采集运行不依赖网络，云语音除外。

## 4. 查找和修改设备 ID

配置入口是 `orangepi/configs/devices.yaml`。修改后重启采集服务；每条 meta.json 会保存当时配置。

| 设备 | 查找方法 | 修改位置 |
|---|---|---|
| 双 EMG 接收接口 | `ls -l /dev/serial/by-id/`，必要时 `udevadm info --query=property --name=/dev/ttyUSB0` | 自动发现支持型号的两套唯一身份设备；先预览 A/B，再人工确认实际左右，结果随录制配置冻结 |
| 四路相机 | `rostopic list`、`rostopic hz /fisheye/left/image_raw/compressed` | `cameras.<left/right/bleft/bright>.topic`；驱动物理节点由 seeker 的 v4l2_isp.yaml 管理 |
| 板载 IMU | `rostopic hz /imu_data_raw`，`rostopic echo -n 1 /imu_data_raw` | `board_imu.topic`；板载芯片没有 EMG 那样的 USB 序列号 |
| 蓝牙键盘 | `bluetoothctl devices`、`cat /proc/bus/input/devices` | `keyboard_name` 必须对应输入设备名，当前 MINI_KEYBOARD Keyboard |

相机消息必须为 `sensor_msgs/CompressedImage` JPEG，当前参数固定 1920×1200、20 Hz；板载 IMU 是 `sensor_msgs/Imu`，约 200 Hz。厂商 YAML 中写 30 不等于实际发布 30；本机实际采集约 20 Hz。更换帧率/协议需要同步修改硬件管线、时间戳和 QC 适配，不能只改一个显示名称。EMG 是当前 29 字节协议、921600 波特率。

只有当前扫描并按 A 确认的槽位参与整次程序。`meta.json.selected_devices` 和 `device_selection.json` 保存确认清单，meta.yaml.recorders 保存实际输出槽位。

新接收器可以更换同类设备的 port；新增第三路 EMG、其他类型 IMU/相机/EEG 等须新增 acquisition 适配、framework 标准化与 checker、元数据，并做真实验证。单纯新增 YAML 项不能宣称支持新协议。此版结构已经为后续适配分类。

蓝牙首次使用需配对、信任、连接（MAC 用新设备实查值）：

```text
bluetoothctl
power on
scan on
pair 实际MAC
trust 实际MAC
connect 实际MAC
quit
```

本机键盘 MAC 是 E6:E9:DA:4D:AC:F3。确保采集用户对串口和 `/dev/input/event*` 有权限；同类镜像通常通过 dialout/input/video/render/audio 组配置，需要重新登录生效。不要用 chmod 777 解决所有设备权限。

## 5. 阿里云语音与 API 更换

使用 [DashScope Python SDK](https://help.aliyun.com/zh/model-studio/qwen-tts-realtime-python-sdk)，当前 `qwen3-tts-flash-realtime`、Cherry、中文、北京 WebSocket 地址。模型、声音和等待时间在 `configs/speech.yaml`；不在代码里写密钥。

```bash
mkdir -p orangepi/private
chmod 700 orangepi/private
# 仅新设备首次创建；已有密钥时不要用示例覆盖。
cp orangepi/configs/secrets.env.example orangepi/private/secrets.env
chmod 600 orangepi/private/secrets.env
nano orangepi/private/secrets.env
```

文件内容为 `DASHSCOPE_API_KEY=你的实际密钥`。以后只编辑这个文件并重启服务；不要把密钥写进 speech.yaml、命令参数、README 或 Git。也支持环境变量 DASHSCOPE_API_KEY（优先于文件）；服务通常使用私有文件。密钥示例、Git ignore 和打包脚本均排除真实密钥。历史备份包含本机私有文件，只能由你保管；对外发源代码包，不发完整私有备份。曾经在聊天中发出的测试密钥建议交付前在控制台换为单独项目密钥。

```bash
source orangepi/scripts/env.sh
orangepi/.venvs/ros/bin/python orangepi/scripts/audio/speech_tools.py test
orangepi/.venvs/ros/bin/python orangepi/scripts/audio/speech_tools.py warmup
```

test 发一次真实请求但不直接播放；warmup 合成固定提示和原 troubleshooting 类别。安全提示音由 `build_safety_prompts.py` 使用阿里云生成，随源码交付在 `assets/cloud_voice`，含 manifest 哈希校验；新机不应删掉该目录。

**不再使用任何本地语音合成或本地声音回退。** 正常语音来自阿里云，已成功生成的云音频可以缓存复用，离线播放缓存仍是阿里云音色。没有可用云音频时：

1. 等待超过 3 秒，用阿里云预先合成的音频播报“请等待，正在获取语音”。
2. 单次云请求最多 35 秒；前台最多等待约 90 秒获取所需音频（另加提示音、错误码的播音时间）。后台按 5/10/20 秒退避重试。
3. API 返回的安全错误码会用预合成的字母/数字音频逐字播报，例如 HTTP_401，不暴露响应头、密钥或完整异常内容。完整安全错误码也写入 runtime/speech_status.json。没有服务端错误码时记录客户端错误类型，不捏造服务端代码。
4. 超时仍不可用时，用云端预合成提示播报超时及安全停止，程序以 78 退出；systemd 对这个状态**不自动重启**，避免循环播报和反复启动。排查网络、密钥、余额/额度、接口区域后手工 `sudo systemctl restart embodied-pi-collect.service`。
5. 关键语音未完成时禁止开始下一段。安全音频缺失/损坏、音频服务不可用也阻止无声采集；日志可用于无音频时排查。Ctrl+C/服务停止可取消等待。

如果已缓存的所需语音可正常播放，即使当前网络不可用仍可继续使用缓存；遇到未缓存的必要提示才执行上述等待/停止机制。模型、声音变更后运行 `build_safety_prompts.py` 重新生成安全音频，检查完成后再部署；不要在采集中覆盖音频资源。

原项目所有匹配的 QC WARN/ERROR 和相关设备排查指南仍在停止后播报，同一条数据中重复的排查建议只播一次，减少无意义等待；B 可重复排查提示。云请求只发送图像名称、状态和提示文本，不上传采集信号/视频。采集中不播放任何声音。

## 6. 启动、开机自启与有屏幕调试

先确认驱动发布、蓝牙已配对、扬声器可播音：`pactl info`。本机 PulseAudio 由桌面启动，已配置自动登录；新机也要让采集用户的音频服务随开机可用。程序尝试当前 PulseAudio、`/tmp/pulse-socket` 和用户运行目录。仅安装 systemd 单元不能代替蓝牙、音频和驱动初始化。

```bash
# 新机/路径改变后安装；以普通采集用户运行，内部按需 sudo。
bash orangepi/scripts/service/install_service.sh
sudo systemctl status embodied-pi-collect.service
sudo journalctl -u embodied-pi-collect.service -f
```

安装器根据当前用户和实际绝对路径生成服务，自动 enable/start；遇到失败约 10 秒重试。本机已启用。服务启动后进入设备确认，尚未开始正式采集。

调试或临时退出：

```bash
sudo systemctl stop embodied-pi-collect.service
# 等停止完成。正在录制会停止、封装和 QC，可能需要几分钟。
bash orangepi/scripts/run.sh foreground --console
# 完整键盘输入 a 或 b 后回车；输入 q 后回车或 Ctrl+C 安全结束。
# 之后恢复正式蓝牙模式：
sudo systemctl start embodied-pi-collect.service
```

`foreground` 不加 `--console` 时仍使用 MINI_KEYBOARD，终端 Ctrl+C 也能结束。完整键盘不会被全局抓取；程序只抓配置匹配的 MINI_KEYBOARD。无 systemd 的手动后台方式：`bash orangepi/scripts/run.sh start`，查看 `status`，用 `stop` 停止；日志在 runtime/launcher.log。不要并行启动手动实例与服务。要永久关闭自启用 `sudo systemctl disable --now embodied-pi-collect.service`；恢复用 `enable --now`。

ESC/q/Ctrl+C 是调试退出，不是正常 A 保留流程；未经 A 确认的会话不标记完成。确认前退出保留条目并标记中断；已按 A 保留且 QC 完成的条目仍可记 used。勿直接 kill -9 或断电，可能损坏未封装 MP4。

不消耗参考图像的硬件试采：

```bash
sudo systemctl stop embodied-pi-collect.service
bash orangepi/scripts/run.sh smoke 12
# 自动扫描、播报、试采、停后等 5 秒、确认、QC；不改 used。
sudo systemctl start embodied-pi-collect.service
```

新结果保存在 SD 卡 `Embodied-Brain-Collect/orangepi-01/diagnostics/时间/`。交付整理已经移走旧测试数据；日后测试后应先查看结果再自行备份清理。程序不会自动删除正式 data。

## 7. 数据目录、名称、格式与原项目差异

完整字段见 [data_compatibility.md](docs/data_compatibility.md)。默认新数据在 SD 卡设备目录下，内部仍使用原班次与会话布局：

```text
/media/orangepi/sdcard/Embodied-Brain-Collect/orangepi-01/data/session-night/YYYY-MM-DD-HH-MM-SS/
  meta.yaml                         原标准会话信息；附 Pi 审计信息
  qc_report.json                    原 QC 报告；配置启用时另有 qc.html
  emg_left/emg_left.npz              原 EMG 标准名称、字段
  emg_right/emg_right.npz
  cam_ros_left/{cam_ros_left.npz,frames.mp4}
  cam_ros_right/{cam_ros_right.npz,frames.mp4}
  cam_ros_bleft/{cam_ros_bleft.npz,frames.mp4}
  cam_ros_bright/{cam_ros_bright.npz,frames.mp4}
  board_imu/board_imu.npz            新增板载 IMU
  marker/marker.npz                 原路径；软件 RUN_START/RUN_END
```

额外审计文件：device_selection.json（已确认设备）、meta.json（含存储配置）、status.json、stop_gate.json、native_capture_summary.json、capture_summary.json、worker.log、embodied_recorder.log；有对应事件时生成 health_events.jsonl/retry.json/controller_error.json。每个硬件槽位 `<slot>.log` 是当前适配器的实际采集摘要，文本内容不等同于原 recorder 的逐行日志。EMG 原始块在 raw_chunks/，附 timestamp_rebuild.json；相机附 timestamps.csv；板载 IMU 附 raw.csv。正式失败条目也会保留；严重失败未完成标准化时，可能只有原始块、CSV、日志和未完整视频，不能假装已有完整 NPZ。

ROS 相机遵循原腕部相机的 `<slot>/<slot>.npz + frames.mp4` 格式，NPZ 含 frames_timestamps；保持 cam_ros_* 区分真实硬件，不改成腕部名称。此版只写 **frames.mp4 一份**。旧数据若同时有 frames.mp4/video.mp4，已查到是同一 inode 的硬链接，不是两段视频，通常不占两份内容；可以用 `ls -li` 核实。没有必要合并、拼接或重新编码，本次不修改历史视频内容。

不能声称所有模态与原项目完全相同：没有 E-Prime/UDP/TTL 硬件发码，所以没有 marker_trial、marker_t_eprime_ms、FIX/IMG/EXEC 事件；board IMU 当前姿态四元数为零、没有有效姿态，保留原值并标 orientation_valid=false；未连接的眼动/EEG/手套/定位/腕带不生成虚假数据。原 pack_episode 对新增 board_imu 尚未验证，当前不自动生成 episode 打包结果。

## 8. QC、已知错误与性能

使用原 BaseRecorder 契约、EMG 时间重建和原 checkers / checker.yaml 阈值；独立数据流并行调度。ROS 相机 20 Hz；USB 腕部使用原 CameraChecker 的 30 Hz 检查；板载 IMU 约 200 Hz。不减帧、不跳过黑帧/冻结检查来换速度。

性能配置在 `orangepi/configs/performance.yaml`：QC 完全在设备停止采集后运行。
2026-09-28 六路相机整套测试在原三工作线程 QC 阶段重启后，默认保守调整为
**CPU 7、一个工作线程**；内存余量 600 MiB、每线程估算 500 MiB、进程虚拟地址空间
上限 1536 MiB 仍保留。数值库/OpenCV 算法线程为 1，正式入口和手动诊断入口均设置
FFmpeg 解码线程选项。未删减质检内容；因此停止后的等待时间可能增加。
采集硬件 MPP 和 QC 的 OpenCV 视频读取是两个阶段，硬件采集不能消除 QC CPU 负载。
不要未经稳定性验证恢复三线程；具体测试结果见 `docs/validation_wrist.md`。

此前 v0.6 三线程的历史 78 秒数据对照：QC 约 91.20 秒，生成报告与原报告完全相同；旧记录的后处理加 QC 为 250.75 秒（两个计时范围略有差别，不能当成严格同范围跑分）。这些是历史性能记录，不能作为当前单线程和六路视频的耗时承诺。长数据仍需更久，另有语音播报时间，不应按固定秒数断电。

最新错误检查（2026-09-24）：相机源时间戳非递增、板载 IMU 时间戳非递增、一次 MPP 管线异常、最终系统盘低于 512 MiB 保护停止。系统盘现有正式数据未删除，删除了可重新生成的旧交付压缩包；新采集和打包均转到 SD 卡。已有日志不足以确定源时间戳异常的驱动根因；已增加前后时间戳、序号和到达时刻诊断，继续严格停止坏数据，不隐瞒错误。详见 docs/diagnostics.md 和 docs/validation_v06.md。

## 9. 备份、整理与 Git 回退

整理前已停止服务，对旧 addon、运行状态、密钥、旧诊断/测试及所有 used.yaml 完整打包，执行 tar 对比校验和 SHA256；另存 Git bundle、旧 service。位置：

`delivery_backups/20260924_before_restructure/`

它包含私人密钥，目录 700、主要归档 600，禁止公开。`CLEANUP_MANIFEST.json` 记录清理范围；`DATA_PATH_MAP.json` 记录历史数据从旧 addon/data 到 data/orangepi-legacy 的搬迁。正式数据保留，旧台账路径随搬迁更新；历史数据文件中的嵌入路径未改写。移除的旧诊断/测试、缓存和历史说明仍可从完整备份追溯。新运行缓存/密钥/数据不纳入 Git。

查看历史：`git log --oneline --decorate`、`git status --short`。回退前先停止服务并备份当前配置及 used.yaml；不要直接 reset --hard。建议把旧版本解到另一个完整目录，确认驱动、密钥和数据路径后重新安装那个版本的服务。旧 tag `pi-v0.4.0-20260923` 使用旧目录名，不能套用新 run.sh 路径；当前版本有独立提交/tag `orangepi-v0.6.1-20260924`。Git 只回退代码，不回退系统驱动、密钥、正式数据或未跟踪台账，完整恢复需结合私有备份。

## 10. SD 卡保存规划、文件命名与拔卡

本机已识别：`/dev/mmcblk0p1`，UUID `4C7F-9FE2`，exFAT，约 256 GB（显示 238 GiB），挂载 `/media/orangepi/sdcard`。未格式化，也未改动卡上原有文件。fstab 已改为按 UUID 挂载，原件备份在 delivery_backups/20260924_v06_before/fstab。

当前配置 `configs/storage.yaml`：

```yaml
mode: sd
mount_point: /media/orangepi/sdcard
expected_uuid: 4C7F-9FE2
device_id: orangepi-01
directory: Embodied-Brain-Collect
shift: session-night
minimum_start_free_mb: 1024
```

保存示例：

```text
/media/orangepi/sdcard/Embodied-Brain-Collect/orangepi-01/
  data/session-night/2026-09-24-17-20-30/   正式记录，沿用原会话命名
    meta.yaml, marker/, emg_left/, ...    仅包含本次确认的采集设备
  diagnostics/时间或测试名称/              明确隔离的试采，不写 used
  delivery/orangepi-source-时间.tar.gz     可复制的程序交付包，不含采集数据
```

- 会话目录为 Pi 本地时间 `YYYY-MM-DD-HH-MM-SS`；同秒已存在会等待下一秒，不覆盖。设备编号隔离不同 Pi；多人多设备时用 orangepi-02 等唯一编号。换班可改 shift 为 session-day 等目录名。
- 图像名称保存在 meta.yaml.environment 和 meta.json.reference_image，不塞进视频名或更改原 NPZ 名称。cam_ros_* / emg_* 的命名和内部格式不因换盘改变。
- 每条开始前验证：实际挂载点、匹配 UUID、与系统盘不同设备、可写、fsync 成功、至少 1 GiB 剩余。检查失败就播报并等待处理，不写回系统盘。
- 采集中低于 512 MiB 或出现 I/O/设备故障会停止。已接受缓存需要落盘；不要录制/保存/QC 中拔卡。先 `sudo systemctl stop embodied-pi-collect.service`，确认停止，再 `sync`，最后 `sudo umount /media/orangepi/sdcard`。exFAT 不是日志文件系统，突然掉电/拔卡仍可能损坏，软件无法保证断电不丢数据。
- 新设备用 `lsblk -f`/`findmnt` 查卡。使用同一张卡和相同挂载位置可保留 UUID；换卡必须将 expected_uuid 改为实查值，并配置新机挂载。不要把系统盘 UUID 填进来。需要格式化新卡时请先独立备份和人工确认，本程序不会格式化。
- 系统盘已有 data/session-night 和 data/orangepi-legacy 暂未搬移；它们的 used 引用仍有效。若以后迁移，先复制并校验，再用下面 rebase 工具改台账路径，最后才考虑清理旧副本。禁止直接移动后丢失路径对应关系。

## 11. 如何修改 used（不是 uesd），新设备是否要改

**继续同一实验：通常不需要单独修改或清空 used。** 打包脚本已经包含当前 configs/environments/**/used.yaml；在新设备解包后会继续跳过已成功采集图片。同一张 SD 卡保持同一路径时，SD 数据引用也保持有效。注意不要用较旧交付包覆盖较新的台账；最好在停止采集后制作最终包。

**开始全新实验：** 先备份，在新项目副本中重置指定场景，不删除原实验台账。以下命令先预览，加 --apply 才执行；执行前必须停止采集，工具会检查启动锁并将旧文件备份到 runtime/ledger_backups。

```bash
source orangepi/scripts/env.sh
# 查看未完成图像
orangepi/.venvs/ros/bin/python orangepi/scripts/storage/ledger_tools.py list
# 示例场景，先预览
orangepi/.venvs/ros/bin/python orangepi/scripts/storage/ledger_tools.py reset-scene --scene 餐桌_倒水
# 确认后执行（保留所有采集数据，只清除此场景 used/failed 台账）
orangepi/.venvs/ros/bin/python orangepi/scripts/storage/ledger_tools.py reset-scene --scene 餐桌_倒水 --apply
```

**只改变数据位置：** 不重置 used。在数据已复制并核对后更新 session 路径前缀：

```bash
orangepi/.venvs/ros/bin/python orangepi/scripts/storage/ledger_tools.py rebase   --old-root /旧位置/data --new-root /新位置/data
# 核对预览后，同一命令加 --apply。
```

工具只改 used/failed 内的 session 路径，不复制数据、不改变 meta 内历史审计字段、不自动把失败改成成功。要将某条记录判为 failed，使用第 1 节 ledger.py failed；SD 卡会话也支持。不要手工把 QC ERROR 的图像标为 used 来跳过检验。

**只运行 package_delivery.sh 是否就够？** 它足以打包项目级必需文件（代码、原框架、配置、当前台账、图库、锁定依赖、云端安全提示音），但无法打包成可直接启动任意硬件的系统镜像。新机还需：厂商内核/相机/IMU/MPP/ROS 驱动；安装 uv 后重建环境；单独安全配置 API 密钥；核对设备 ID、SD UUID/挂载、设备编号；蓝牙配对、音频启动；最后安装自启服务。需要已有采集数据或完整 Git 历史时分别复制 SD 数据和 Git bundle。密钥不在包里是有意设计，不要为了省一步把它放回公开配置。

## 12. USB 3.0 腕部相机：规格、左右映射和重新查找

本次选用两台 `USB Camera3`（USB ID `0bda:5858`），实际 USB 链路均为 **5000M**。
当前沿用原项目双腕 **640×480、30 fps、MJPG 输入**。此前选定的 720p30 在整套测试中发生重启，暂不作为默认配置。设备还声明支持 640×480、
1920×1080 等尺寸及 30/60 fps；程序当前只接受已接入原 QC 的 30 fps，不应仅根据
设备标称支持就改成 60 fps。规格调整后必须做整套设备试采，不能只看相机能否打开。

原项目 `configs/recorders.yaml` 两个腕部槽位设置 `kind: opencv_camera`、`hz: 30`，
没有覆盖宽高或 fps，因此使用
`src/embodied_brain_collect/recorders/camera/camera_recorder_config.py` 中
OpencvCameraConfig 的 `640×480 / 30 fps / MJPG` 默认值。`hz` 是轮询上限，
真正向相机请求的帧率来自 fps。新项目可以沿用这组规格；更换 USB 3.0 相机不要求提高分辨率。

640×480 的每帧像素数为 1280×720 的三分之一，可降低硬件编解码及内存带宽负载，
但 USB MJPEG 实际码率取决于画面和相机压缩，不能承诺总功耗也降低三分之二。
本次腕部从一开始就使用 MPP 硬件解码和编码，没有通过软件 OpenCV/libx265 处理这两路。
硬件加速不能排除供电、USB Hub、内核/MPP 驱动问题；既有重启没有持久化上一启动日志，
pstore 为空，无法认定为 CPU、过热或内存不足。实测边界和未解决事项见验证记录。

两台 USB 2.0 相机 `05a3:9230` 单独可出流，但同一 USB 2.0 总线上同时启动，
在 MJPG 640×480 和 320×240、30 fps 下均复现 `VIDIOC_STREAMON: No space left on device`。
内核同时记录 `Not enough bandwidth for altsetting 6`，确认是该拓扑下带宽分配失败，
不是 SD 卡满。按用户要求停止使用该组合；它们可保持连接，但不在采集配置里。

当前左右映射已经用户确认，绑定的是 Hub 端口：

| 槽位 | USB 端口 | 当前 video 节点（仅供核对） |
|---|---|---|
| `cam_left_wrist` | `1.4.4.1` | `/dev/video84` |
| `cam_right_wrist` | `1.4.3.4` | `/dev/video86` |

配置位于 `orangepi/configs/devices.yaml` 的 `wrist_cameras`：

```yaml
wrist_cameras:
  cam_left_wrist:
    label: 左腕相机
    device: /dev/v4l/by-path/platform-xhci-hcd.9.auto-usb-0:1.4.4.1:1.0-video-index0
    width: 640
    height: 480
    fps: 30
    fourcc: MJPG
  cam_right_wrist:
    label: 右腕相机
    device: /dev/v4l/by-path/platform-xhci-hcd.9.auto-usb-0:1.4.3.4:1.0-video-index0
    width: 640
    height: 480
    fps: 30
    fourcc: MJPG
```

**不要把 video84/video86 当永久编号**。重启/插拔可能重编号；同型号设备的序列号
可能重复，`by-id` 会碰撞。`by-path` 固定端口，但换 Hub、换插口后也必须重新查找。
同一台相机可能有 image/metadata 两个 video 节点，选择能枚举 MJPG 的 `video-index0`。

停止采集后查找：

```bash
cd /home/orangepi/Desktop/Embodied-Brain-Collect-main
sudo systemctl stop embodied-pi-collect.service
python3 orangepi/scripts/diagnostics/list_cameras.py --formats
lsusb -t
v4l2-ctl --list-devices
ls -l /dev/v4l/by-path/*usb*video-index0
```

根据工具输出的名称、USB ID、5000M 速度和端口，把对应 `device` 完整路径填写到
上述左右槽位。装配时按当前映射贴好左右标签；换线后用遮挡镜头方式核对，不能凭
video 编号大小判断左右。修改后运行有效数据扫描和隔离试采：

```bash
source orangepi/scripts/env.sh
orangepi/.venvs/ros/bin/python orangepi/src/orangepi_collect/acquisition/scan_devices.py
orangepi/.venvs/ros/bin/python orangepi/scripts/diagnostics/smoke_capture.py \
  --seconds 20 --slots cam_left_wrist cam_right_wrist cam_ros_left cam_ros_right \
  cam_ros_bleft cam_ros_bright emg_left emg_right board_imu
# 确认测试完成、查看输出会话内 qc_report.json，再恢复正式服务
sudo systemctl start embodied-pi-collect.service
```

试采只写 SD `diagnostics/wrist-日期时间/`，不修改 `used.yaml`。设备扫描会实际同时
接收腕部 JPEG 帧；确认后，每段仍重新预检。相机掉线、队列满、编解码停滞、视频帧数
与时间戳不一致会报错，不能只凭文件存在判定成功。

正式保存结构保持原项目约定：

```text
/media/orangepi/sdcard/Embodied-Brain-Collect/orangepi-01/data/session-night/YYYY-MM-DD-HH-MM-SS/
  meta.yaml
  cam_left_wrist/cam_left_wrist.npz
  cam_left_wrist/frames.mp4
  cam_right_wrist/cam_right_wrist.npz
  cam_right_wrist/frames.mp4
  cam_ros_left/ ... cam_ros_bright/
  emg_left/ emg_right/ board_imu/ marker/
  qc_report.json
```

腕部 NPZ 主字段 `frames_timestamps` 是 float64 Unix 秒，每帧一项，使用主机收帧
时刻；新增 `arrival_unix_ns`、`arrival_monotonic_ns`、`video_pts_ns`、`source_pts_ns`
为 int64 审计字段。它与 ROS 相机 header 时间来源不同，不代表硬件同步。
`timestamps.csv`、`video_check.json`、管线和日志用于排错；视频为硬件 HEVC/MP4。
两路帧数可能因起止边界相差一帧，不补帧或伪造时间戳。

## 13. 程序打包、原始数据打包与数据集转换的区别

- **程序迁移**：`bash orangepi/scripts/package_delivery.sh`。现在明确列出
  orangepi 的 src/configs/scripts/tests/docs/assets/README 白名单；临时文件放入
  runtime，不会随程序包带走。输出仍在 SD `delivery/`，附 SHA256。
- **完整原始会话交付**：停止采集并等待 QC 完成后执行下列命令，保留全部原始文件、
  板载 IMU、诊断与相同相对路径。不会转码、重命名内部文件或修改 used。

```bash
source orangepi/scripts/env.sh
orangepi/.venvs/ros/bin/python orangepi/scripts/storage/package_data.py \
  /media/orangepi/sdcard/Embodied-Brain-Collect/orangepi-01/data/session-night/具体日期时间
```

输出为 `delivery/data/orangepi-data-orangepi-01-会话名-打包时间.tar.gz` 和 `.sha256`。
包内从 `data/session-night/会话名/` 开始；诊断会话则从 `diagnostics/会话名/` 开始。
在新设备的 `Embodied-Brain-Collect/orangepi-01/` 目录解包即可恢复相对布局；路径
不同则按第 11 节更新 used 中的绝对路径。解包前确认没有同名旧会话，避免覆盖。

- **mf_lerobot 转换**：原 `scripts/pack_daily.py` 自动发现 `cam_*` 视频，本次腕部
  命名满足其输入约定；需要另装 mf_lerobot，并显式将 `--source` 指向 SD 的
  `data/session-night`、`--out` 指向目标数据集目录。其板载 IMU 映射尚未补齐，
  本次不将转换包当作完整原始数据备份，也不在停止录制后自动运行转换。

目录评估详见 [layout_assessment.md](docs/layout_assessment.md)，数据对照详见
[data_compatibility.md](docs/data_compatibility.md)，实机结果见
[validation_wrist.md](docs/validation_wrist.md)。原根 src/configs/scripts 保持原接口，
不复制第二套参考图库或 used 台账。
