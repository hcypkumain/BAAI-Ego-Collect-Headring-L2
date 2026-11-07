# Android USB 手机采集页面

手机开启 USB 调试，用数据线连接 Pi，在首次授权弹窗允许此 Pi，建议勾选“始终允许”。服务建立 `adb reverse tcp:8765 tcp:8765`，非录制状态下尝试打开 `http://127.0.0.1:8765/phone`。

不需要手机与 Pi 连接同一 Wi-Fi，不要求提前写手机绑定。原绑定作为优先选择；旧手机拔掉后可连接并授权新手机。多手机且无法唯一选择时拒绝任意选取。不会使用网络 ADB 设备，也不会复制别的 Pi 的 ADB 密钥。

首次 Android 授权不可绕过，iPhone 不适用。部分 Android 系统限制 ADB 拉起浏览器，可手动打开网址并输入此 Pi 的访问码。网页鉴权没有被关闭；自动打开时通过 URL fragment 传递本机访问码，页面消费后移除它。

## 同型号 Pi 安装

保留原有私有 adb 运行库，或先安装与当前系统匹配的 `adb`、`plugdev` 支持。新环境通常可执行 `sudo apt install adb`，不要为此升级内核或整个发行版。

`scripts/install_pi_services.py --apply` 安装脚本、服务与仅针对 Android USB 调试接口的 udev 权限规则，并创建属于本机 orangepi 用户的状态目录和缺失的 ADB 密钥。不会重新生成已存在的密钥，不自动启动采集或重置 USB。

完成依赖和安装后：

```bash
sudo systemctl enable --now usb-phone-link
journalctl -u usb-phone-link -n 30 --no-pager
```

服务只向已授权 USB 手机提供本机 8765 页面。未插手机、只用充电线、未授权或 Pi 采集服务未就绪时，状态不等于可正常采集。安装 udev 规则后，新手机插入会获得权限；已插入手机若仍无权限，正常拔插一次数据线，不要重置整个 USB 集线器。
