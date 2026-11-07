# Android USB 手机采集页面

安装程序必须先启用 Pi 的 USB **数据主机**策略。手机开启 USB 调试，用数据线连接 Pi，在首次授权弹窗允许此 Pi，建议勾选“始终允许”。服务建立 `adb reverse tcp:8765 tcp:8765`，非录制状态下尝试打开 `http://127.0.0.1:8765/phone`。

## 数据角色不是供电角色

USB-C 直连可能协商成“手机主机、Pi 外设”，这时手机不会收到 Pi 的 ADB 授权请求。`headring-usb-data-host` 在安装、开机和再次连接时检查并处理这一情况：

- 有 USB-C 对端时，只通过标准 `port0/data_role` 请求 Pi 成为 `host`。
- 不写 `power_role`、`port_type` 或 `preferred_role`，不强制反转供电方向。
- 未连接对端且此控制器没有其他下游设备时，可以初始化该控制器的 host 模式；有对端时禁止用 debugfs 强制替代正常协商。
- 使用采集状态/隐私守卫及各模式的 capture lease；正在录制、保存、准备或状态不可信时不主动切换。
- 对端拒绝时按间隔重试，单次连接最多三次，不循环重置 USB；拔插后按新的连接重新处理。

这是软件守卫，不意味着可以在正式录制期间随意插拔 USB-C 或腕相机；物理插拔和内核协商本身也可能影响传输。固定 USB 主机口同样可以连接手机。对不支持标准角色切换的设备会明确报告限制，不伪造 host 状态。

不需要手机与 Pi 连接同一 Wi-Fi，不要求提前写手机绑定。原绑定作为优先选择；旧手机拔掉后可连接并授权新手机。多手机且无法唯一选择时拒绝任意选取。不会使用网络 ADB 设备，也不会复制别的 Pi 的 ADB 密钥。

首次 Android 授权不可绕过，iPhone 不适用。部分 Android 系统限制 ADB 拉起浏览器，可手动打开网址并输入此 Pi 的访问码。网页鉴权没有被关闭；自动打开时通过 URL fragment 传递本机访问码，页面消费后移除它。

## 同型号 Pi 安装

保留原有私有 adb 运行库，或先安装与当前系统匹配的 `adb`、`plugdev` 支持。新环境通常可执行 `sudo apt install adb`，不要为此升级内核或整个发行版。

`scripts/install_pi_services.py --apply` 默认、不可选地安装并启用 `headring-usb-data-host.service`，立即执行一次安全的数据主机初始化；随后由守护服务处理开机/再次连接。安装计划也包含相机、采集控制器和 USB 手机服务的启动顺序依赖。

它创建属于本机 orangepi 用户的状态目录和缺失的 ADB 密钥，不重新生成已有密钥，不自动启停采集/相机/媒体，不复位整个 USB 集线器。若采集或初始化尚未结束，安装会要求先结束该流程。首次 Android 授权必须由用户确认，数据角色调整不会自动点击“允许”。

完成依赖和安装后：

```bash
sudo systemctl enable --now usb-phone-link
journalctl -u headring-usb-data-host -n 30 --no-pager
journalctl -u usb-phone-link -n 30 --no-pager
```

服务只向已授权 USB 手机提供本机 8765 页面。未插手机、只用充电线、未授权或 Pi 采集服务未就绪时，状态不等于可正常采集。安装 udev 规则后，新手机插入会获得权限；已插入手机若仍无权限，正常拔插一次数据线，不要重置整个 USB 集线器。

安全状态记录在 `/run/headring-usb-data-host.json`，不含访问码、ADB 私钥或手机内容。`host` 表示角色已符合策略；`deferred`、`partner_refused`、`unsupported` 等表示仍需等待或使用 USB 主机口。

## Windows/离线新头环安装包

`tools/provision/install.py` 和 `device_setup.py` 是同一版本的新机安装器源文件。新机激活采集程序前必须启用数据主机守卫，并核验 `state=host`；不再仅依赖最后人工切换。

从既有内部安装包保留 ARM64/Python 依赖，生成一个新的统一版本安装包：

```bash
python tools/update_headring_bundle.py --bundle /path/to/old-internal-bundle --output /path/to/new-bundle
```

该构建器不覆盖原包，不复制任何设备的 ADB 私钥或任务进度。生成包会保留原包的内部网络/语音配置，不能把其 payload 或模板公开推送 GitHub。已有设备不能用“新机安装”覆盖，需走保留配置的数据主机升级流程。
