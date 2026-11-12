# 阶段3—5实施、验收与回滚

以下是后续工程任务，不是本轮已实施。main单owner协调，Sol做下一阶段，禁止因分支已备份就部署。所有生产操作仅在明确单机/数据/网络窗口进行；未定的用户policy不能用代码默认值填空。

## 阶段3：第一条最小垂直切片（确定范围）

**交付链：fixture controller state → immutable device snapshot → local pure GET → 现fleet_monitor投影 → fleet cards及40177只读消费。** 无START/STOP、任务配置/导入写、云连接/上传、设备部署。先以历史脱敏fixture shadow compare，不调用生产旧status来“比对”。

### 代码落点及Git边界

Sol独立branch建议`feature/system-read-model-v1`，从评审确定的R只读基线开始；不得从phone分支整体合入。R backup01bd501保持原状。因为Q非Git根且已有服务运行，**不得直接编辑Q运行文件**。在该branch新增 `host/qc/` 作为现Q组件的可追溯打包源：只复制下表允许文件，记录original absolute path+SHA256的`host/qc/SOURCE_MANIFEST.json`，不复制config/private/secrets/state/log/raw、.git、venv。这不是第二个聚合器部署；最终安装仍替换同一Q fleet_monitor组件，阶段3只离线运行拷贝。源归档需要main审核，不能把Q所有目录git add。

|未来文件（R相对路径）|具体实施责任/禁止依赖|
|---|---|
|`contracts/v1/*.schema.json`、`contracts/v1/fixtures/`|导入本设计schema和合成fixture；严格nullable/unit测试，版本兼容表|
|`orangepi/src/orangepi_collect/control/snapshot_contract.py`（新增）|纯函数将已有publisher输入归一化；未知不推断；不导入硬件/存储目录扫描|
|`.../control/snapshot_publisher.py`（新增）|不可变对象、boot/controller实例/seq、原子pointer；仅接受显式已采样值；阶段3不启resource sampler|
|`.../control/dashboard.py`|增加授权后的`GET /api/v1/snapshot`，读取publisher pointer；无query；旧display/status路径不借用；publisher从既有update的缓存输入得到缺省unknown|
|`.../control/launcher.py`|仅注入controller生命周期身份及把已有事件/缓存传给publisher；不改begin/finish/scan/owner行为；不能为了填字段调用status或worker探测|
|`host/qc/fleet_monitor/{server.py,agent.py,history.py,recovery_history.py,thumbnail.py}`|来源分别Q同路径；首批只改server新route与隔离注入点；旧agent/thumbnail不得成为v1 fallback；源副本保留供后续迁移|
|`host/qc/fleet_monitor/read_model.py`、`snapshot_adapter.py`（新增）|现Hub内部组合使用，不另启服务；fixture输入、来源分栏、out-of-order/重启/age处理；不构造History/Store，生产polling关闭|
|`host/qc/receiver.html`|来源Q/receiver.html（不是index.html）；切片新增v1卡片渲染函数和fixture入口，磁盘/RAM/unknown/stale/来源分开展示；无控制按钮、新媒体和任务编辑|
|`tools/fleet_maintenance/{web.py,static/app.js}`|新只读v1消费适配；fixture/local注入或同源代理host model；不得调用refresh/check/SSH获取卡片状态；旧维护jobs仍独立标时点|
|`orangepi/tests/test_snapshot_contract.py`、`test_snapshot_read_purity.py`|不实例化真实硬件controller，以依赖注入local HTTP handler测授权、payload、GET纯度|
|`host/qc/tests/test_read_model.py`、`test_fleet_read_purity.py`、`test_fleet_cards.cjs`|投影、host route、UI使用实际新代码和fixture，失败不得把unknown变green|

若现handler绑定使纯读测试需要启动`Hub.start`/History构造器，应先抽出显式注入的read-only handler依赖；不能用“测试环境”去初始化真实Q路径。只运行loopback测试端口、临时fixture路径；禁止调用生产localhost40177/8788/8791路由，因为GET可能启动监看。

### 首批可验收测试（必须通过）

1. **结构/语义**：所有本目录正反fixture；missing字段、额外secret、负数、NaN、重复JSON key、错device/boot/instance/seq、任务互斥计数失败。null RAM不得显示0GiB；UTC不可信不得计可信今日。
2. **读纯度**：mock/spies将privacy.display、demands.touch、subprocess、socket outbound、scandir、raw open、statvfs、hardware probe、History.sync与文件write设为调用即失败；100次GET/304/无效query/未鉴权不改变registry/owner/需求/cache。允许仅内存HTTP连接及流订阅计数，不允许观测触发scheduler。断连也不注册display。
3. **host单模型**：fixture poll事件→Hub投影→两个消费者得到同instance/revision/payload。旧seq/旧instance晚响应拒绝；host重启恢复stale；源DB unavailable不扫描raw；SSE溢出reset后GET恢复。首批可以内存投影，无需上线SQLite新表。
4. **shadow**：冻结的phase1事实fixture与v1并列，输出差异矩阵：known一致、legacy缺失unknown、旧推断discarded、冲突reason。禁止要求旧接口的错误推断与新schema“相等”。至少15已证时钟/role/policy缺口和其余四机unknown都覆盖。
5. **UI**：5张设备卡分别unknown/known/stale/error/reboot；设备不可达不显示stopped，磁盘和RAM独立，版本安装/运行未知，源/age可展开。暂不展示未实现的可执行START/STOP、导入、删除或cloud retry。
6. **resource预算**：阶段3真实采样保持unknown；publisher高频读不额外采样，序号只随发布更新。资源sampler预算测试放阶段4A，避免为卡片偷偷加SSH。

通过后main/Sol审查归档代码与离线测试证据；此时只完成阶段3离线实现，不宣称设备已经提供新API。回滚=停用branch fixture入口/新route功能开关，生产未变；兼容旧API和历史账本不删除。

## 阶段4：按安全边界拆批，不做巨包

|批次/owner|文件级落点及验收|回滚与放行条件|
|---|---|---|
|4A 观察发布/设备+host负责人|C snapshot_publisher + 新resource_sampler；Q投影版本表/固定调度；旧agent遥测退役；RAM5s、disk30s、blocked statvfs只有一项在途、GET零IO、read-only receiver adapter|新采样任务关停仍提供unknown；旧模型只作历史，不能恢复副作用poller作为v1 fallback；只离线直到5A|
|4B ownership/控制负责人|C launcher、dashboard action、privacy、camera registry及worker IPC；新增command journal/reducer单元；逐条审查旧候选补丁而非整包合并|灰度禁用v1写入口，不自动重放未决START；保留journal/ledger和旧owner状态；先满足下述故障测试|
|4C 数据链/receiver负责人|C session封存与reference context；Q handoff_receiver.py、receiver_state.py、raw_handoff.py；QC/normalizer catalog；pc_retention.py/worker仅显式禁删门禁|停止新消费者、保留临时文件/receipt供恢复；不downgrade schema覆盖已有ledger，不启自动清理|
|4D USB任务/任务负责人|C usb_task_import.py、reference_controls.py、catalog；R tools/fleet_maintenance imports|preview/commit/revision/profile指纹和断电恢复、ro fallback拒绝；回滚catalog/preferences只能基于journal已知hash，不触碰U盘源|
|4E 发布/认证/phone/host负责人|组件BOM、bundle installer事务、Q service恢复import打包、角色scope迁移、phone独立允许清单|先plan+offline staged target；保护配置/身份/标定/源；不能拿tether完整branch做collector升级|

4B必须有可重复fault/model tests：两client重复/不同hash；START返回false；privacy取消；journal fsync失败；断电在accepted/dispatch/effect间；deadline/late ACK；STOP满队列优先；孤请求超时不堵别的session；32+8+64容量及64MiB/预留空间耗尽；cancel terminal压缩重放；boot重启/PID复用；impedance/scan/prepared/STOP竞争；热插拔role失效；callback风暴和日志慢盘；锁内IO failpoint；锁图/线程dump。测试应证明给定模型中的安全性与可达恢复，不能把mock通过写成硬件无死锁保证。

4C使用合成文件验证所有提交断点及hash冲突：rename前后、receipt前后、ACK丢失、QC更正、重复attempt、normalized catalog失败、云receipt重放/对象不一致、时钟倒退、错误卷/满盘/IO错误。云测试用本地fake，真实对象stat/download另窗口。设备删除和PC删除各自test默认false、拒绝缺副本证据；不以测试通过授权删除。

### release bundle事务要求

bundle携带每组件source/target/hash/mode/account/dependency/schema-read/write范围、approval、保护路径清单、pre/post manifest、可回滚payload。部署前确认实际身份/权威idle+stop proof、空间及受保护配置/标定历史的hash快照。只读preflight可报blocked，不能擅自修复。staging验证→同文件系统atomic switch→启动/读取runtime manifest→健康与功能验收→记录applied receipt；跨组件无法原子切换时journal记录每步并使用兼容窗口，不宣称跨服务事务原子。

中断后按applied journal、目标post-hash与实际runtime恢复，不按计划猜已应用。回滚只恢复本bundle拥有且仍匹配post-hash的文件；遇后续人工修改停止并报告。保留session/command ledger、raw、receipt、设备身份、校准历史、网络私有配置；新schema只做additive migration，旧reader不能读时停用消费者，不回退覆盖数据。source备份与production approval必须分别记录。tether包仅独立transport允许清单+经审collector公共改动，不能发备份祖先所有文件。

## 阶段5：明确现场窗口、单机后扩展

所有下列真实动作均需用户给出对象与窗口；本设计不自动执行、不自动全机。先一台身份清楚且可安全idle的设备，单手机，恢复原版本材料齐全。观察到其他四机仍unknown不触发扫描。

|窗口/矩阵|现场动作与通过证据|失败处置|
|---|---|---|
|5A 单机只读API|核已登记Family地址、安装批准组件、验证GET不增加摄像头owner/demand，测CPU/RAM/IO与age；UTC前后偏差只观察，校时另批准|停新poller/rollback代码，保持unknown|
|5B 控制/硬件|START→privacy ACK→实际采样→STOP→proof→sealed；阻抗取消/过期、scan争用、断连、CPU/媒体负载；记录STOP≤预算与超时unknown|先恢复权威stop状态，不抢owner/继续试START；超预算不放行|
|5C 手机两条独立链|当前ADB基准；另窗口启用tether，调试关闭、无SIM/互联网、OEM手动开页、拔插/换名/多接口/错selector、锁屏/后台/Wake Lock、参考图/预览负载和长按STOP|验证Family/Guest原路由DNS保持；冲突/重叠/浏览器不可用回批准ADB流程，不自动切换|
|5D USB任务|只读挂载helper权限、preview/commit、拔盘/文件变动/revision冲突、切profile、断电恢复（用测试U盘）|hold、不改U盘原件、不覆盖不明catalog|
|5E storage/数据恢复|明确4TB和12TB身份关系，在独立测试目录验证错误卷/满盘/IO错误/fault-hold；合成及经授权样本恢复hash|不修盘或删除源；未有双副本恢复证据保持禁删|
|5F 云|仅批准scope精确对象stat/hash/download恢复，不全桶扫描；断网/ACK丢失/重复upload验证|保留host custody与原件，本地receipt仍不能称live可恢复|
|5G 多机推广|单机观察通过后下一台，混版本schema/多client/host重启、Family/Guest带宽与CPU预算；每台独立manifest/role/clock/rollback记录|任何身份/role/存储/stop失败停止扩展，不批量重试全部设备|

每一窗口产出明确applied manifest、runtime证据、测试动作/时间、失败和回滚结果。手机离线33+12通过、历史236hash、GitHub归档都不能替代此矩阵。未决用户policy只阻塞相关业务放行，不阻塞阶段3只读开发。
