# 第三方组件与来源

- `src/embodied_brain_collect` 来自 Pi 上的原采集框架及现场修改。原导出 commit 记录在快照清单中，原说明/CHANGELOG 保留。
- OmniBCI Python SDK 通过 `orangepi/scripts/setup_eeg.sh` 中的原发布地址和 SHA256 安装，不复制整套虚拟环境。
- `vendor/wheels/robot_uploader-1.0.0-py3-none-any.whl` 为用户提供、Pi 当前使用的上传器。发布包元数据没有声明许可证，本仓库不额外授予第三方代码许可。
- `vendor/pi-runtime/egorecord` 为 Pi 上的 camd 运行文件、配置、IQ 数据及设备树参考，不包含完整 EgoRecord GUI。
- `vendor/pi-runtime/opi-headphone-audio/guarded-paplay` 为设备上当前使用的 aarch64 原生播放器，Pi 上未找到其源文件。
- ROS、GStreamer、Rockchip MPP、系统驱动和通用 Python 包仍按其各自来源和许可使用。

本次归档不改变原作者或厂商的权利、许可与使用条件。运行文件的来源、哈希和源码可用性以清单为准。
