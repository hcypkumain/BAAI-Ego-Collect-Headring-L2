# v1 契约（规范性设计目标）

关键词“必须/禁止”为后续实现验收条件，非现状保证。JSON字段精确定义见schemas；对象拒绝额外字段，schema_version为固定`1.0`。新增字段发布新minor schema并协商，消费者不能无审查把新字段写入日志。单位固定bytes/ms/UTC RFC3339，整数计数非负，unknown是null或显式unknown枚举，绝不能伪造0/false。

## 1. 设备snapshot、host fleet与时间

`GET /api/v1/snapshot`：同原HTTP授权，无query；200返回device schema，即使部分采样错误也保留200+quality/error；没有发布对象返回503 `SNAPSHOT_UNAVAILABLE`。不能注册display、touch demand、renew监看、读raw/scandir、probe/开摄像头、采样statvfs、启动线程/进程或写cache。GET `/api/v1/commands/{request_id}`同样仅读receipt投影，未知返回404，不执行恢复。ETag绑定controller_instance/seq；可以304但age由客户端自身时钟继续增长，不把304当新设备采样。

`GET /api/v1/fleet`：Q现server新增纯读路由，必须在旧`hub.touch()`之前dispatch；全量host schema，不调用History.sync或设备网络。`/api/v1/fleet/events`基于已运行投影事件，不启动遥测；事件id=`host_instance:revision`。旧routes标legacy，兼容期不宣称纯读。首次phase3仅fixtures驱动、无生产poller。

### 字段来源（同一组共享sample，组内独立未知值保留null）

|组/字段|权威来源/语义|
|---|---|
|device_id / boot_id / controller_instance / seq|登记稳定设备ID；Linux boot UUID；每次controller启动的新UUID；该instance内递增发布序号。不是PID，不跨instance比较seq|
|time.utc / utc_trusted / monotonic_ms|发布时设备CLOCK_BOOTTIME对应毫秒（实现统一选BOOTTIME，禁止混MONOTONIC）；墙钟是否经同步及偏差阈值验证；unknown trust=null。时长以同boot单调差，不用UTC|
|sample.source / boot_id / sampled_mono_ms / age_ms / stale_after_ms / quality / reason|每组证据出处及采样新鲜度；quality=`known/unknown/stale/error`。known不代表组内每一nullable字段已知。sample.boot必须与当前boot匹配才能known；旧数据可保留stale|
|mode / lifecycle / recording_active|controller模式及worker明确录制ACK；不能由lifecycle字符串推导录制false；mode仅eeg/emg/unknown|
|session.context|session_id、task_group_id、ref_id、asset_sha256、list_id/list_revision、task_policy_id/version、profile_id/profile_epoch全链稳定；legacy缺失为null，不拿路径/标题hash替ref_id|
|progress|设备ledger在同scope revision下的count；host不覆盖；定义见下|
|resources|disk_volume_id为脱敏逻辑卷ID，不是盘serial；disk_free_bytes=f_bfree×f_frsize，disk_available_bytes=f_bavail×f_frsize；ram_mem_available_bytes=MemAvailable×1024，不是MemFree；采样分别附sample|
|health.camera / eeg / impedance|只读owner已有状态；quality可unknown。阻抗measurement_id、owner_run、session_id、quality_version、can_review/reason；已知身份/恢复/保存失败为false，无权威判定为null。监控stale不改变测量资格；已完成保存结果不因等待确认而固定TTL过期|
|roles|左右每项binding_id为随机公开引用，不是序列号hash；confirmed和calibration_valid可null；calibration_version、profile_epoch及proof_boot_id，不暴露物理身份原文|
|version|base_commit / overlay_manifest_sha256 / installed_manifest_sha256 / runtime_manifest_sha256 / approved_release_id分开；hash未知null，不以源码代替安装或运行|
|usb_transport|configured/current、state、reason、sample；addressed只代表网络地址，不是手机身份/已注册/录制ready；不输出selector、serial、token|
|queues|device receipt_pending、handoff_pending、failed_attempts；unknown=null。host另外有receiver/QC/normalized/cloud队列|
|host device row|last_seen_utc由主机接收时间给出，last_seen_age_ms为本host_instance单调计时，connectivity与snapshot质量分开。失联保留snapshot，不能把设备事实改idle|

设备不能知道主机last_seen，不在device伪造。host `source_clock_trusted`通过嵌入snapshot.time呈现；host自己的time另列。host reboot时旧缓存row的age=null、connectivity=unknown，直至新接收。host收到同instance旧seq丢弃；不同instance先身份验证再替换，旧实例迟到响应丢弃。boot未变controller重启也必须新instance。跨机monotonic从不相减；host新鲜度=自己的elapsed+设备报告age。unknown monotonic不能fresh。UTC不可信的day stats标`day_basis=untrusted_device_clock`，不并入可信“今日”；按host接收日统计必须明确另命名，不改录制发生日。

### 任务计数的唯一口径

scope=(device_id, list_id, list_revision, profile_epoch, task_policy_version)。unit=该scope内稳定ref_id的一次目标完成槽；重采attempt不会增加total。`total = completed + pending + reserved + remaining`，四项互斥：completed=现行可追溯规则/ledger明确接受的完成槽；pending=raw已封存、待QC/裁定；reserved=当前预留但尚未封存；remaining=未预留且未完成的槽（失败释放后可回到这里）。failed_attempts单独累计，WARN/ERROR计数是attempt结果统计、不从remaining重复扣除。仅scope或完成规则无法确定时相应计划计数null、reason=POLICY_UNKNOWN；可独立展示真实saved sessions数量。task-block分组策略缺失不使已有完成规则/ledger失效，禁止默认block_size=5。

本轮不新增或改变PASS/INFO/WARN/ERROR的完成政策。明确存在的现行规则及ledger结果按原口径只读发布；不能因WARN标签自行取消完成，也不能单凭WARN标签自行计完成。manual多列表聚合推荐固定明确revision范围，无法确定时仅相关汇总为unknown。离线封存是否进入pending以现行可信流程为准，QC/交接回执必须身份匹配。任何后续policy变更单独审批并产生新version，不追溯重写历史ledger；统计按来源/规则版本分组。

## 2. 最低必要命令协议（阶段4才接入）

不沿用“一个孤立ticket占全局pending”的设计，也不自造预授权票据链。复用controller单一reducer和持久化小命令journal：HTTP请求就是意图，privacy现有黑屏ACK仍是必要的效果门禁，不替换其因果绑定。

`POST /api/v1/commands` operator鉴权；body固定 `request_id` UUID、`command` START/STOP/CANCEL、`device_id`、`expected_boot_id`、`expected_controller_instance`、`target_session_id`、`expected_state_revision`整数、`deadline_ms`（100..30000）、`target_request_id` nullable（仅CANCEL必填）。START的target_session_id必须是controller已分配且上下文冻结的prepared session；STOP必须明确session，不能“停止当前任意session”。CANCEL取消的是目标request，不能暗中作为新session STOP。客户端用已发布instance发命令，任何旧instance请求409，不自动重放START到新实例。

START/CANCEL严格比较expected_state_revision。STOP的revision仅作审计，instance/boot/session必须精确相同且当前仍拥有该session；不能因录制过程中revision自然增长拒绝停止。已经停止的同session可用保留的匹配proof幂等成功，无proof则unknown，不能把另一个session的idle当证明。deadline从服务器接收计时，不依赖客户端UTC。

服务器对这些业务字段（不含request_id和认证信息）使用UTF-8、sort_keys、紧凑JSON、禁止NaN/重复key、SHA256计算intent_sha256；v1字段仅string/int/null，因此无需自造跨语言浮点canonicalization。幂等键=(device_id, authenticated principal内部ID, request_id)，同键同hash返回原receipt，不同hash409 `IDEMPOTENCY_CONFLICT`。principal不公开。请求体≤4KiB；重复键拒绝400。只要求一个持久journal事务原子写intent+accepted记录，不实现分布式exactly-once幻想。

```text
请求到达(未持久，不代表accepted)
  → rejected[终态，无执行] 或 accepted[fsync成功，返回202]
accepted → executing → succeeded | failed | cancelled | unknown
accepted → cancelled | rejected | unknown
unknown → succeeded | failed | cancelled（只能经绑定证据reconcile）
```

receipt的`execution`与`effect`独立：execution=`not_started/running/completed/unknown`；effect=`not_checked/verified/contradicted/unknown`。succeeded必须effect verified并带匹配instance/session的proof；收到202或worker“收到”ACK不是成功。START proof=实际采样开始ACK+privacy arm因果匹配；STOP proof=worker停止采集ACK+owner_run/generation+privacy stop proof，sealed证明另记ledger。STOP succeeded不代表文件已完成QC。取消前若START已产生实际采样，CANCEL返回`TOO_LATE`，保持privacy，另发明确target session STOP。cancelled必须证明未开始且释放reservation；终态取消/拒绝不可被后来的ACK改成功，迟到ACK归独立异常并reconcile当前owner，必要时触发原有隐私保护，绝不reveal。

### 容量、恢复与超时

v1设计容量：普通active最多32（每principal最多4），同session START最多1；STOP独立8槽按session合并（多个request各有receipt映射）；终态明细4096条或7天（先达者压缩），未决unknown最多64，不占普通运行槽但同目标session禁止新START。每个request终态hash/tombstone保留到对应controller_instance退休且重放窗口24h结束；普通新请求候选限额为每principal速率1/s、burst4，**不能使用该普通限额拒绝安全STOP**，STOP走独立合并/保留预算。以上数值是待离线负载及现场验证的初始设计预算，不是现有性能保证。超过容量503 `CAPACITY_EXCEEDED`+retry_after_ms，不能逐出未决/取消记录。journal达到64MiB后拒绝新START；STOP仍走预分配独立journal空间（8MiB），并可立即发安全stop intent但持久失败不得称accepted/succeeded，只报503 `JOURNAL_UNAVAILABLE`、effect unknown。STOP专用空间满时告警并等待维护，不通过无限增长或删除证据解锁。

压缩仅保留request_id/hash/终态/session/proof摘要，过明细窗口重复返回410 `RECEIPT_EXPIRED`，不再执行；同instance退休后所有写请求instance校验先拒绝。活跃instance超过窗口仍保存键的tombstone直到实例退休；容量耗尽拒新START，而非重用旧键。一次孤请求的deadline触发reconcile，释放普通队列位置，保留session gate；其他session/设备不被全局堵塞。未知owner对应实际硬件仍可阻塞共享硬件START，这是安全阻塞而非请求票据泄漏。

journal restart扫描未决intent，结合当前boot/owner/session ledger判定：不能证明则unknown，绝不自动重发START；STOP可在相同session/owner仍活跃时恢复调度，否则unknown/verified依据证据。HTTP超时客户端用相同request_id查询或重发相同body，不生成新START；客户端断开不取消执行。deadline使用接受时单调计时，同boot运行有效；重启deadline不可延续猜测，进入reconcile。晚ACK只有identity/generation全匹配且不违反terminal约束才接受。STOP始终先于普通命令排队；网络不可达时UI必须显示“停止未证实”，不能给假绿灯。

## 3. 数据事务和保留

每个session冻结context（schema定义），manifest记录context hash、raw文件相对路径/size/SHA256及sealed ledger revision。`session_id`使用独立随机稳定ID，不由墙钟生成；legacy session name通过只读映射保留为legacy_id，不能按同名跨device合并。ref_id是业务asset身份、asset_sha256是内容身份，两者都贯穿raw→host→QC→normalized→cloud。

|迁移事件|提交条件/权威|失败恢复与禁止项|
|---|---|---|
|raw_sealed|设备worker关闭文件，manifest+ledger事务提交/fsync|中途断电为interrupted，恢复封存检查；不冒充完成|
|received_host_custody|receiver临时路径接收、精确manifest/hash验证、正确卷、文件及目录fsync、原子rename、签收持久后发exact ACK|rename后receipt前崩溃：重验并补receipt；receipt后ACK丢失：重发同receipt；不得按“传输成功”删设备源|
|qc_final|QC job_id/attempt_id绑定session/raw manifest/规则版本，结果事件可重放|重复job同hash幂等；新hash冲突隔离，失败attempt不变新session|
|normalized|确定性profile版本+输入hash→package manifest；先staging再rename/catalog commit|catalog失败重建索引，不覆盖raw；保留lineage，标准包不是raw副本|
|cloud_confirmed|精确bucket/key/version/size/hash清单及验证方法、时点、scope写receipt|本地签名只证明本地签收来源；不等于今日cloud live或可下载恢复|
|retention_eligible|设备/PC独立policy、授权、可信时间、正确卷、hash、两份独立可恢复副本证据均满足|eligibility不执行delete；当前两端deletion_authorized=false|

两份副本必须不同失效域，记录volume/object identity、manifest hash、最近恢复演练及验证时间；raw与normalized不等价，12TB盘和4TB盘关系未证不能算两份。签名验证是receipt可信来源检查，不替代对象live stat/hash或恢复演练。保留期限从可信host custody/云签收时点和授权policy计，不以慢55h设备墙钟判过期；host时钟不可信则hold。任何source删除都需单独事务授权与审计，本轮/阶段3无删除。

USB任务导入是operator/maintainer写任务，不是观测：`POST imports/preview`（scan亦是显式作业）返回plan_id、bundle_sha256、volume_fingerprint、文件identity(dev/ino/size/mtime)+hash、expected_list_revision/profile_epoch、目标差异和只读挂载证据。commit必须再次核对这些指纹及hash、身份、revision，409任何变化；幂等键=(bundle hash, target list, profile epoch, import mode)，相同返回原revision，变更需新预览。ZIP解析限目录/体积/成员数、拒绝链接/穿越/重复名/炸弹；只读源、复制到独立staging，永不搬走或改写U盘原件。fallback已挂载卷缺ro证明返回 `USB_READONLY_UNVERIFIED`，不隐式remount；需用户窗口核mount helper。现pending journal old/new catalog hash机制保留并补profile/plan fingerprint，catalog和preferences恢复只允许已知old/new hash，否则hold，不盲覆盖。目录fsync与journal删除顺序必须断电注入验证。

## 4. 鉴权、错误与观测日志

observer仅snapshot/fleet/history/授权metadata；operator显式START/STOP/CANCEL及授权任务导入；maintainer维护计划/部署/网络和恢复。使用既有HTTP授权设施演进scope，不加第二自制token/ticket；新snapshot不能弱于旧授权。display注册、privacy ACK及媒体另走明确写/按需接口，禁止塞进GET snapshot。thumbnail按(device,boot,controller,session,ref_id,asset_sha256,list_revision)身份绑定且每次验证privacy公开资格；cache命中也不跳过门禁，不在status内嵌原图/帧/EEG。阶段3不实现新媒体路径。

统一错误body=`{schema_version:"1.0",error:{code,message,retryable,retry_after_ms,correlation_id}}`，message脱敏短文本，code稳定；receipt.error使用同error对象或null。HTTP错误不要求一定已有request receipt（认证/持久化之前拒绝）。

|code|HTTP / 处置|owner|
|---|---|---|
|AUTH_REQUIRED / FORBIDDEN|401/403，重新认证/缺scope|HTTP/auth|
|INVALID_REQUEST / IDENTITY_MISMATCH|400/409，改请求/核登记，不能放宽identity|client/controller|
|INSTANCE_CHANGED / SESSION_MISMATCH / REVISION_CONFLICT|409，刷新并人工重新确认意图，不自动START|controller|
|IDEMPOTENCY_CONFLICT / TOO_LATE|409，查询原receipt/明确STOP|command reducer|
|CAPACITY_EXCEEDED / JOURNAL_UNAVAILABLE|503，按retry_after退避；STOP未证实提示|command journal|
|OWNER_BUSY / OWNER_UNKNOWN / EFFECT_TIMEOUT|423/423/504，等待释放/维护，不抢锁|owner协调器|
|PRIVACY_NOT_READY / ROLE_PROOF_INVALID / MEASUREMENT_STALE|409，恢复对应证据，不绕门禁|privacy/calibration|
|POLICY_UNKNOWN / USB_READONLY_UNVERIFIED|409，显式政策/挂载证明后重试|task/import|
|STORAGE_UNAVAILABLE / MANIFEST_CONFLICT|503/409，暂停写/隔离，保留源|storage/receiver|
|SNAPSHOT_UNAVAILABLE / RECEIPT_EXPIRED / NOT_FOUND|503/410/404，保留unknown/查ledger/不执行|read-model|

日志结构allowlist：event_name, correlation_id, request_id, measurement_id, scan_id, receiver_job_id, device_id, session_id, controller_instance, boot_id, seq, owner_run, component/version, state_from/to, reason_code, age_ms, duration_ms。跨action/measurement/scan/receiverjob显式关联父correlation_id，不能靠时间拼接。禁止token、nonce、serial、原配置、raw数据、原图/完整路径、未经筛选异常文本。非阻塞有界队列1024，单event≤2KiB，10MiB×5轮转，drop计数暴露health；安全关键receipt写journal而非靠日志。先加观察事件及因果contract，再改变动作路径；日志永不成为权威state替身。

measurement的controller绑定在publisher接受worker事件时验证：同boot controller重启后，清空旧measurement与can_review，不允许仅因相同boot/TTL就接受旧值。v1 wire中impedance组继承顶层controller_instance；publisher禁止把不同controller的measurement挂在当前snapshot下。相应测试必须覆盖同boot重启；若未来需要展示旧measurement，另设history接口并显式携带原controller_instance，不能混在当前can_review内。
