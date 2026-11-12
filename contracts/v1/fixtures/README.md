# 合成契约样例

这里的UUID、hash、计数均为合成示例，不是15号的现场值；尤其device.json的有效标定、可信UTC不代表现机已经修复。

- `device.json`：完整合法snapshot；`host.json`：合法host模型，设备/云unknown、指定卷missing；`command.json`：合法STOP效果证明。
- `cases.json`：在base上递归覆盖对象、数组整体替换、null保持为值；可先替换合成UUID。`valid:false`是应拒绝的结构或跨字段反例，不是合法错误响应。
- 合法错误状态：device-storage-error、host-receiver-error、command-timeout-unknown。
- 重启：device-reboot-role-invalid、host-reboot-cache；旧proof反例command-reboot-proof-invalid。
- 过期：device-expired-resource、host-expired；unknown：device-unknown-policy-and-RAM、host-unknown。

从本目录父目录执行 `python3 validate_examples.py --show device-reboot-role-invalid` 可在stdout得到完整wire JSON（不写文件）。无参数验证全部JSON语法、4个Draft2020-12 schema和25个样例预期，使用本机已有jsonschema，不联网加载`headring.invalid`；该域名仅作schema ID。

验证器的跨字段检查是文档验收辅助，不是生产validator或完整协议model tests。命令历史转换、终态不回退、容量恢复、锁序、无副作用与IO预算必须由后续实现测试验证，详见MIGRATION。安装jsonschema不可用时只报告语法已验和schema未验，不安装依赖、不冒称通过。
