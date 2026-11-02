# 部署说明

此快照面向当前 Orange Pi / Linux aarch64 系统。先准备与现场兼容的内核、相机驱动、
Rockchip MPP/GStreamer 插件、ROS Noetic 的 Python/GI 包以及 `/dev/icm20602` 驱动。
这些系统依赖没有被伪装成可在任意电脑上通过 pip 安装的包。

## 1. 初始化本机设置

`tools/configure_station.py --collector-id <编号>` 只创建缺失的本机选择文件，保留已有设置。
核对 `orangepi/configs/storage.yaml` 的挂载点、UUID、设备目录和 shift；核对 `devices.yaml` 的硬件类型。
当前配置中的物理串口标识是导出设备的配置，新机器应通过发现与左右确认流程适配。

导入自己的任务 ZIP。仓库的 `configs/environments/` 只有说明文件，不含现场 2.3 GB 任务图库。

## 2. Python 环境

安装 `uv` 后运行 `orangepi/scripts/setup_envs.sh` 和 `setup_eeg.sh`。
硬件环境使用系统 Python 3.8 的系统包；框架与脑电使用独立 Python 3.11。
`deployment/manifests/runtime-versions.json` 是现场版本清单，仅用于对照，不能将其中所有系统包盲目替换为 PyPI 包。

上传进程当前查找 `/home/orangepi/maintenance/uploader/venv/bin/python`。同样的 `orangepi` 用户环境可执行：

```bash
uv venv --python 3.11 /home/orangepi/maintenance/uploader/venv
uv pip install --python /home/orangepi/maintenance/uploader/venv/bin/python \
  vendor/wheels/robot_uploader-1.0.0-py3-none-any.whl
```

该 wheel 的依赖也需要安装。主硬件环境需有 `cryptography` 以保存本机上传凭据，
以及当前程序需要的 numpy、Pillow、PyYAML、GI、GStreamer 和系统串口包。
语音依赖为 `dashscope`，密钥仅保存在本机私有目录。

## 3. 服务与运行文件

`scripts/install_pi_services.py` 默认只打印计划；`--apply` 才复制文件、备份被覆盖文件并重载 unit 定义。
它安装预览代码、四个采集相关 unit、drop-in 和两条项目专用 udev 规则。
它不会启动/停止采集、触发 USB 重置、覆盖 PulseAudio/Avahi 全局配置或格式化 SD 卡。

已安装匹配的 EgoRecord HAL 时，可使用其现有 camd。需要恢复仓库中的 aarch64 运行文件时，
额外指定 `--with-vendor-runtime`；该选项会安装 camd、其私有库、当前 IQ 文件以及 guarded-paplay。
仓库没有这些厂商二进制的原始 C/C++ 源码，也没有完整 GUI 依赖包。先使用 `ldd` 检查动态依赖。

```bash
python3 scripts/install_pi_services.py --with-vendor-runtime
sudo python3 scripts/install_pi_services.py --apply --with-vendor-runtime
```

安装计划会将原 Pi checkout 路径替换为当前仓库绝对路径。
为避免 systemd 命令转义歧义，部署路径应使用不含空格的普通绝对路径。
`deployment/os-reference/` 及其他平台 udev 规则只供核对，不自动应用。

## 4. 启动前核对

确认没有正式录制或上传任务后，按实际设备处理旧服务所有权：旧 `seeker_cam_bridge` 应保持停用，
避免与 camd 同时占用相机和 3A 锁；EgoRecord GUI 不应同时占用相机。

在依赖和存储核对完成后启用所需服务：

```bash
sudo systemctl enable --now camd
sudo systemctl enable --now embodied-pi-collect pi-phone-media embodied-pi-diagnostics
systemctl status camd embodied-pi-collect pi-phone-media --no-pager
```

手机整页刷新以加载新脚本。按页面完成本次 USB 枚举下的腕相机、肌电左右确认和脑电采前流程。
开始、停止、保存以 Pi 返回的状态为准。

## 5. 更新现有设备

先结束正在录制/准备的段及后台上传，再备份代码和本机配置。比较待更新 diff，
不要覆盖 `runtime/`、`private/`、任务图库、录制目录或上传回执。
本次发布仅上传仓库快照，没有用部署工具修改当前 Pi 的采集服务。

已有设备从旧服务名称升级时，先按 [媒体服务迁移说明](MIGRATION_PHONE_MEDIA.md) 完成切换。
不要在采集回调仍运行时只更换共享内存的消费者端。
