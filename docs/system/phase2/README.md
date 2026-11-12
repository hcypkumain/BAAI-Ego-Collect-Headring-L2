# 头环系统阶段2：统一架构与契约

**推荐：保留现有录制器和控制器，以模块化单体收敛状态、命令和数据保管链；复用 QC fleet_monitor 作为唯一主机只读模型维护者。** 本目录是可实施设计，不代表实现、生产批准或现场验收。下一步是阶段3离线只读垂直切片，而非再合并一个 START 补丁。

## 决策摘要

|保留|收敛/迁移|废弃目标（兼容期后）|
|---|---|---|
|collector/controller、worker、privacy、硬件唯一 owner|控制器内统一 snapshot 发布器和命令 reducer；不另建微服务|监控使用 `/api/status`、观察端 SSH 扫 raw、从 mode 推断已停止|
|Q fleet_monitor 的卡片、历史和 SSE 基础|一个 host read-model；40177消费它，维护作业仍在 broker|第二份“设备实时真相”、GET续期采集需求、历史字段混写|
|设备 ledger、receiver 原件/签收、云精确签收|稳定 session/ref/list/profile 身份贯穿，来源分栏；保留现行完成口径|把本地 cloud 字段当云实时证明、仅凭WARN标签或重试次数推导完成|
|哈希清单、身份/标定保护、严格主机身份|组件级 source/installed/runtime/approved 四份证据|“备份分支=可部署”、236 collector 文件=全系统版本|
|ADB 有线当前路径及独立 tether 候选|手机链作为独立组件灰度；保留 Family→QC、Guest→云|自动切链、广泛网卡选择器、隐式 Wi-Fi/有线网络替代|

本轮证据继续分 **已证 / 待证 / 设计目标**。15号 phase1 时点文件匹配 f5+36d805、时钟慢约55h、左右证明无效、无 task-block policy；其他四机当前事实未知。指定4TB盘未挂载、12TB关系和云可恢复性未证。当前不删任何设备或PC源数据。历史14/18/22同overlay、20未装，不改写为今天状态。

手机 `feature/phone-usb-tether@3c9541244a7801bc247d68df73012260f3999e9f` 的33 Python和12 UI通过属于输入的离线证据；本轮读到该提交文档与之相符，未重跑、未部署。其祖先含备份/WIP，不能整包发版。

## 阅读顺序与验收

1. `ARCHITECTURE.md`：谁拥有事实、谁能写、锁与组件清单。
2. `CONTRACTS.md`：字段语义、状态机、事务和错误；JSON Schema只验证结构，跨字段约束以此为准。
3. `schemas/` + `fixtures/cases.json`：版本1精确字段和正反样例；`python3 validate_examples.py` 离线验证，不导入运行代码，不安装依赖。
4. `MIGRATION.md`：Sol阶段3文件级切片、影子验收和阶段4/5门槛。
5. `DECISIONS.md`、`EVIDENCE.json`：取舍、原始锚点、未决用户政策。

**阶段2完成标准**：schema可验证；工程师能直接写无副作用GET、因果命令、容量恢复、ownership和数据事务失败的contract tests；每类风险有owner、事实源、处置与迁移顺序。不是保证现有实现已无死锁。

manual多列表汇总的范围以及任何新的完成政策需要明确，但**本阶段不改变现行WARN完成规则，也不重算历史账本**。能够从现行ledger/规则明确取得的统计照实发布并标来源；只有无法确定scope或规则时才标unknown。不存在task-block分组文件不等于没有完成账本或所有进度都未知，不能默认5张。保留期限/删除授权当前明确关闭，未来另定。

阶段3不需要重新讨论是否写设计文档：main协调Sol独立branch实现下面确定的离线切片，独立审查后归档本设计。现场设备、网络、服务、数据库写、部署与真实采集另需显式窗口。本轮未实施任何这些操作，也未commit/push。
