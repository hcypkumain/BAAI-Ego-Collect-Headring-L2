# BAAI Ego Collect · 脑电采集版（CP13）

基于 Orange Pi / RK3588 运行程序整理的手机采集项目，已同步 **2026-11-06** 最新采集程序及三台设备验证过的 `headring-common-20261106-v3` 公共模块。
包含主采集框架、手机控制页、六路视频、肌电/IMU、任务 ZIP 导入、QC、云端上传及配套服务。
本次版本的功能、质检规则、环境迁移和验证范围见 [11 月 6 日版本说明](docs/RELEASE_2026-11-06.md)。
本分支 `feat/eeg-cp13-sync-20261106` 维护 13 号脑电版本；`main` 继续维护肌电版本。
脑电、阻抗检查及原有通道/滤波协议保留，并合并公共 QC、肌电错误说明、任务切换和预览修复。详见 [脑电版更新说明](docs/EEG_RELEASE_2026-11-06.md)。

| 路径 | 内容 |
|---|---|
| `orangepi/src/orangepi_collect/` | Pi 上当前使用的采集、控制、预览发布、存储和 QC 集成 |
| `src/embodied_brain_collect/` | 原始采集框架与检查器，含现场使用的修改 |
| `services/phone-media/` | 手机页面的本机预览、历史回看、参考图和耳机管理后端 |
| `orangepi/tests/`、`tests/` | 当前测试代码 |
| `deployment/systemd/`、`deployment/udev/` | Pi 上生效的服务及规则快照 |
| `deployment/os-reference/` | PulseAudio、Avahi、平台 USB/音频脚本参考；不自动覆盖操作系统 |
| `vendor/pi-runtime/` | 当前 camd、图像配置及音频播放器运行文件 |
| `vendor/wheels/` | 用户提供的 `robot_uploader-1.0.0` 安装包 |
| `deployment/manifests/` | 公共版本哈希、原快照来源、组件清单与历史环境版本 |

## 当前采集链路

- 头环四路从 **camd `/run/camd.sock`** 获取 H264，不启动旧 ROS 摄像头桥。
  当前配置输出为 1920×1200、30 fps；这不是对传感器原生最大分辨率的声明。
- 两只 USB 腕相机使用独立的 USB 拓扑路径，当前配置 1920×1080、30 fps。
  实际保存尺寸和帧数会再核验；预览降画质不改变录制分辨率。
- 板载 IMU 从 `/dev/icm20602` 获取；双臂肌电按开机插入顺序确认左右。OmniBCI 脑电由独立 Python 3.11 SDK 进程采集，保留 CH1–8、250 Hz 及阻抗检查。
- 保持现有采集模态，不额外录制麦克风音频；语音文件用于流程播报。
- 每段手动开始，黑屏确认后倒计时。停止后等用户选择：确认保存才整理与 QC；重采会删除当前放弃段并重新准备同一任务。
- 信号数据保留原始异常证据，不伪造硬件同步精度。已验证的稀疏肌电拼接修复保留原文件并重新执行原始框架检查，WARN 可继续，ERROR 保留。
- 历史列表排除已删除及明确放弃的段，并重新过滤缓存。正常 ERROR 记录不因错误级别被自动隐藏。
- 多个启用列表组成混合任务池，隐藏列表仅供查看；来源标签及本段场景资料随采集保存。使用与验证见 [手机任务池与页面稳定性](docs/MIXED_TASK_POOL.md)。

硬件进程使用系统绑定的 Python 3.8 / GI 环境 `.venvs/hardware`，框架使用 Python 3.11。
当前启动入口不依赖 ROS。内核、camd、MPP、GI/GStreamer 等硬件系统依赖仍需保留。

## 部署到同型号 Pi

先阅读 [部署说明](docs/DEPLOYMENT.md)。仓库不包含整个操作系统镜像或完整 EgoRecord GUI。

```bash
git clone --branch feat/eeg-cp13-sync-20261106 https://github.com/hcypkumain/BAAI-Ego-Collect-Headring-L2.git
cd BAAI-Ego-Collect-Headring-L2
python3 tools/configure_station.py --collector-id L3-001
bash orangepi/scripts/setup_envs.sh
python3 scripts/install_pi_services.py                # 先查看安装计划
sudo python3 scripts/install_pi_services.py --apply   # 安装配置，不自动启停采集
```

使用工作站自己的采集员/设备编号，并核对 `orangepi/configs/devices.yaml`、`storage.yaml`。
脑电设备的本地 `eeg.serial` 必须按本机填写；仓库示例留空，升级现有站点时保留原 devices.yaml、标定和运行偏好。
脑电设备的本地 `eeg.serial` 必须按本机填写；仓库示例留空，升级现有站点时保留原 devices.yaml、标定和运行偏好。
脑电设备的本地 `eeg.serial` 必须按本机填写；仓库示例留空，升级现有站点时保留原 devices.yaml、标定和运行偏好。
脑电设备的本地 `eeg.serial` 必须按本机填写；仓库示例留空，升级现有站点时保留原 devices.yaml、标定和运行偏好。
脑电设备的本地 `eeg.serial` 必须按本机填写；仓库示例留空，升级现有站点时保留原 devices.yaml、标定和运行偏好。
脑电设备的本地 `eeg.serial` 必须按本机填写；仓库示例留空，升级现有站点时保留原 devices.yaml、标定和运行偏好。
现有 SD 卡不会被格式化；UUID 与挂载点不匹配会阻止录制。

手机入口：`http://<Pi当前IP>:8765/phone`。热点 DHCP 变化后使用新 IP；
热点宿主 vivo 手机不保证能解析 `.local` 域名。

耳机连接 Pi；手机的“总览 → 耳机设置”或“连接设置 → 蓝牙耳机”支持无屏配对、保存选择、自动重连和状态显示。
详细步骤见 [蓝牙耳机使用说明](docs/HEADPHONES.md)。

## 数据、账号与配置

任务图片、采集视频/信号、运行日志、虚拟环境、上传凭据、屏幕令牌和开机校准证据不入库。
任务 ZIP 在手机“参考图库”导入。开机校准须按当前接线重新完成，不能复用另一台设备或另一轮 USB 枚举的证明。

本机选择保存在 `orangepi/configs/reference_control.json`、`headphone_selection.json`、`audio_output.json`；
它们由初始化工具/页面创建，Git 仅提供对应 `*.example.json`，不会携带旧任务列表选择或蓝牙地址。

语音服务使用本机 `orangepi/private/secrets.env`，按 `configs/secrets.env.example` 配置并设置权限 600。
云端上传 AK/SK 在手机输入，存入 Pi 本机加密凭据库；不放进 Git、不通过命令行传参。

上传支持多选、今日批次、进度与失败重试。已上传数据的本地清理要求成功回执和未变化的本地清单；
远端核验为文件集合及大小，不宣称远端 SHA256 内容核验。

## 验证与参考

```bash
python3 tools/verify_repository.py
node orangepi/tests/test_screen_sync.cjs
node orangepi/tests/test_recording_overlay.cjs
# Linux 依赖环境下的隔离媒体接口测试，不连接采集设备：
orangepi/.venvs/hardware/bin/python orangepi/tests/test_phone_media_service.py
```

完整硬件测试应在依赖就绪的 Pi 上执行；部分原框架测试需要自行导入的任务图片或具体硬件。
本次上传不在运行中的 Pi 上启动录制或重跑采集设备测试。

仓库已移除独立终端路线，手机通过 8765 访问采集界面；媒体后端只监听本机 8766。
旧设备升级服务名和共享内存目录前，请阅读 [手机媒体服务迁移](docs/MIGRATION_PHONE_MEDIA.md)。

- [快照范围与已知限制](docs/SNAPSHOT_SCOPE.md)
- [厂商接入文档](docs/integration/hal_integration.md)：文档与实际 camd 二进制有接口差异，当前实现以 `camd_source.py` 为准。
- [原框架文档](docs/framework/README.md) 与 [Pi 历史说明](orangepi/README.md)
- [第三方运行文件说明](THIRD_PARTY_NOTICES.md)

当前仓库不是原始录制数据或任务完成进度的备份；这些数据需要另外保存。
