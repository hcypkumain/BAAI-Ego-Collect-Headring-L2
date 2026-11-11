# v8.5 稳定性说明：完整性、停止与任务库事务

## 记录范围

本文件为源码修复记录；实际设备版本和部署结果以事务回执及最终发布记录为准。

Astra 总体代码发布审查为 GO，21 号受控代码部署审查为 GO。该结论不代替设备事务回执；本轮尚未实机录制或拔线验收。

## 变更摘要

| 范围 | 行为 |
|---|---|
| 任务库 | 延续 v8.3 选择性合并、删除、恢复和刷新幂等；新增场景文件不可变事务，替换/并发冲突不会覆盖旧克隆引用的字节。 |
| 采集资格 | 肌电/脑电模式分别冻结 9/10 路必需设备合同。确认、START、QC、完成记账和上传共用同一资格；失败或待 QC 的资料可留作诊断，但不计正常完成、不进入标准上传。 |
| 历史资料 | 完整 legacy 录制可重新执行完整 QC 并签发新回执；缺少历史在线证明时明确记 warning，不伪造合同或设备证明。 |
| 主机交接 | 签名回执绑定 passed、输入、native 摘要、request、reference 与资格结果；主机托管的完成台账继续保留完成事实。 |
| STOP | 缺停止 gate 时不进入 KEEP 等待；倒计时中 STOP 立即生效，START 超时会发 STOP；只有原 owner 进程组确认退出才恢复失败 UI。SD 写入失败时仍在 `finally` 发布真实 Dashboard 快照；清理未确认时禁止开始下一段，正常慢收尾不在 45 秒后强杀。 |

普通质量判定继续使用原框架和现有硬件适配，不添加第二套质量阈值。上游参照为 `Koorye/Embodied-Brain-Collect@9966975`；头环快照的 CameraSet、ContainerIntegrity 与首尾 0.1 秒检查包含本地版本差异，不宣称与上游 `latest` 完全一致。

## 验证证据

- 真实历史完整数据完成 full QC，结果为 WARN；损坏 MP4 在原 `FrameCountMatch` 检查报 ERROR；缺少四路必需流报 ERROR。19 份真实 selection 缺四路均被拒绝；1080 种 report 类型变异无异常。仅修改 receipt 元数据仍可验证，改动视频内容则验证失败。
- `work/luna-host-integration` 的旧数据跨目录流程通过新 full QC WARN、apply、本机与 portable receipt 校验及幂等验证；缺绑定的旧 host 结果和输入变更两类负例均被拒绝，源树未变。
- STOP 专项 30 个 Python 用例、6 个 JS 用例（含真实 safe-idle 路径）通过。最新 host 修复后的专项结果：qualification 8 项及 6 个子测、custody 9、receiver 4、lifecycle 18 及 4 个子测、commit 7、device 15 及 8 个子测通过。12 个 CJS 模块由 Node 单轮执行，fail=0；原始日志和 EMG validation 记录见 `work/final-cjs/run.log`、`work/final-cjs/emg-validation.json`。EMG 浏览器 fixture 的 13/13 assertions 通过，修复仅补隐私/display 注册，Dashboard 生产代码与 HEAD 相同。
- handoff 签名绑定 passed、输入、EEG gate hash 与 host tombstone；非默认 profile custody 已统一使用共享 `CODE_BASE`/HMAC，并严格校验当前 `PROJECT` 和 mode，独立 8 项测试通过。Astra 总体代码审查为 GO；设备交接是否可用仍以对应事务回执确认。
- 已存广回归为 817 项：741 通过、49 失败、4 个环境收集错误、23 跳过。旧 speech 行为 fixture 仍不匹配 async 接口；`gi` 与 `omnibci` 缺失导致部分模块无法收集，因此不宣称全测试通过。逐模块统计和 HEAD 差异见 `work/luna-final-regression/summary.md`。

## 部署验证边界

部署准备 `work/deploy-cp21` 的 10 个本地 mock 测试通过，21 号受控代码部署审查为 GO；是否已应用及设备版本以该机事务回执和最终发布记录为准。留存的 21 号设备只读快照为 idle、无 worker，camd PID 2831；更新范围限定该机代码并保留配置、任务、标定和数据。PC 8788 的本轮只读记录为既有 `STORAGE_FAULT_HOLD`、仅提供 `fault_dashboard`；本轮未修改、重启或清除此状态。须匹配新主机协议并完成存储恢复后，才能依据主机回执确认可接收新 handoff。

当时 USB 只读挂载的可选 helper 缺失，`lsblk` fallback 可用，系统规则未改。底层 USB 与 18 号相机已有硬件问题不在本轮解决范围内。
