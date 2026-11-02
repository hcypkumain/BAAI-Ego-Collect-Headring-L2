# 快照范围

来源为 Pi 当前 `Embodied-Brain-Collect-main.2` 的框架与 `orangepi` 代码。
另一个 `Embodied-Brain-Collect-main/orangepi` 是指向该目录的符号链接；
另一个 checkout 根目录中不同的 73 个框架/脚本/测试文件单独放入 `legacy/alternate-project/`。

现场代码不只在 Git 已跟踪文件中。本次同时收录未跟踪的正式源码、手机页面、测试、
系统中独立部署的预览/回看程序、实际 unit/drop-in、项目相关 udev 规则及必要运行文件。
不复制旧 `.git` 历史，因此不会把历史提交中未知的数据或密钥一并镜像到新仓库。

`deployment/manifests/pi-source-snapshot.json` 记录原文件路径、大小、SHA256 和原权限。
根 README 与原 gitignore 为发布需要重新编写，原版分别保存在 `docs/framework/`。
三份本机选择配置改为提供模板。`tools/verify_repository.py` 对这些明确的发布转换单独核对。

排除范围：原始录制、任务图片及已导入图库、日志、运行状态数据库、虚拟环境、缓存、
密钥、凭据库、令牌、开机左右确认/阻抗证明、旧安装备份以及可从标准依赖获取的整个 Python 环境。
标准 pip/system 依赖记录版本，不复制其整套 site-packages。

`camd`、`librkaiq_d`、`guarded-paplay` 和设备树运行文件在 Pi 上只有二进制；
仓库保存它们的实际运行副本及哈希，未声称包含其不存在的源代码。
完整 EgoRecord GUI、系统内核、MPP 驱动源代码也不在这份设备源码快照中。

当前应用修复包括 camd 接入、预览/录制分离、ZIP 导入、辅助元数据 QC、
脑电最终门限处理、保存后再 QC、弃段删除与历史缓存过滤、手机多批次上传和腕相机启动诊断。
腕相机偶发启动取流失败的底层根因尚未确定；增加日志并不等于已证明是供电或线材故障。
