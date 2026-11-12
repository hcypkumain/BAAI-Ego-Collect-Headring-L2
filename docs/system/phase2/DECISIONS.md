# ADR 决策记录

全部状态为“阶段2推荐设计，待main/Sol实现审查”，不授予生产操作权限。

|ADR|决定及理由|代价 / 退出条件|
|---|---|---|
|01 模块化单体|在现controller分离reducer、owner、publisher；不重写recorder、不引入分布式服务|需枚举旧锁与callback；只有实际隔离需求和测量证据才拆服务|
|02 唯一host投影|Q fleet_monitor维护read-model；40177消费+维护broker|Q源码需先纳入可追溯source bundle；receiver DB保留，不能把“统一”理解为合并业务数据库|
|03 读纯度|新v1 snapshot/fleet只取缓存；旧status不作monitor fallback|旧设备字段unknown是可接受结果；保持旧手机接口兼容直到迁移|
|04 时间与身份|boot+controller UUID+seq排序；BOOTTIME计时；UTC另判可信|缓存重启后先stale；不允许“今日”把不可信设备日混入可信统计|
|05 命令最小协议|一个request+幂等hash+journal+效果proof；不增加预授权孤票据|需要有界压缩和恢复测试；exactly-once不宣称；unknown可保留安全session gate|
|06 STOP与owner|优先mailbox，时间预算，超时unknown；不kill owner抢锁|硬件卡死仍需维护窗口；安全停与快速恢复不可混同|
|07 数据保管链|设备/host/cloud各自权威receipt；raw不可被标准包替代|跨源投影更复杂，但禁止local receipt伪装cloud live；当前不删源|
|08 任务policy|稳定ref/list revision/profile epoch；无分组policy不猜5；保留现行完成ledger|manual聚合范围及新政策另审批；不因本架构设计改变WARN规则或重算历史|
|09 部署审批|组件source/install/runtime/approval分开；事务bundle保护数据与标定|236 collector hash不足全系统；backup/tether祖先WIP不能整包上线|
|10 网络/手机|Family设备QC、Guest云；tether独立有线链、窄selector、禁止默认路由/DNS接管|connected-route重叠和手机浏览器限制需真实窗口；不自动fallback|
|11 TLS/认证|先host可信入口与边界核验，再设备角色凭据灰度；手机证书另试点|普通HTTP不可假称安全/支持Wake Lock；不突然使现登录失效，不用不安全flags|
|12 日志不是事实库|非阻塞脱敏事件+独立ledger/receipts|日志drop允许、receipt丢失不允许；观测先于改变动作|

## 真未决政策与技术待证

- **用户政策**：manual多列表聚合范围及任何新完成规则需另审批；当前沿用可证现行规则/ledger，仅无法确定的字段标policy_unknown。缺分组文件不代表所有完成统计未知。删除当前关闭，保留期不是本轮默认授权。
- **现场待证**：四机实时身份/版本、15时钟同步源与标定、完整auth/TLS边界、4TB/12TB关系及双副本恢复、真实云receipt、NM/手机型号和Wake Lock、完整锁图与性能预算。它们不阻止阶段3本地fixture切片，阻止相应阶段5放行。
- **实现选择已决定**：不再让工程师选择第二聚合器、第二控制协议或以SSH填unknown；用ARCHITECTURE定义的模块和schemas定义的字段。若发现无法满足的旧API约束，应记录新ADR而不是隐式加fallback。
