# Fleet code and USB maintenance

## 已登记 EEG 阻抗增量（仅 CLI）

默认 `--release stable` 仍是原 236 文件版本。另有固定登记的 `--release eeg-impedance-advisory`（完整 ID `eeg-impedance-advisory-36d8059918ce`）；资源和五文件前后哈希受固定 SHA 校验，不接受自定义 root 脚本。14/15/18/22 曾应用并验证，20 未应用，历史结果不证明当前状态。工具不扫描子网；地址须由用户确认，SSH 仍验证该设备原有 known-host 身份及远端 DEVICE_ID。保存在 `~/.cache/headring-fleet-maintenance/inventory.json` 的地址是缓存，不是在线证明。默认仅对明确选定的单设备执行；不支持隐式发现。

以下仅为示例；重新操作前须独立授权并确认设备身份与地址（两个命令使用同一个已确认地址）：

```sh
python3 tools/fleet_maintenance/fleet.py --release eeg-impedance-advisory --targets cp0022 --address cp0022=确认的IP
python3 tools/fleet_maintenance/fleet.py --release eeg-impedance-advisory --targets cp0022 --address cp0022=确认的IP --apply --accepted-plan 上一步输出的计划SHA256
```

第一条是只读计划，第二条从本机交互终端用现有 SSH/pexpect/getpass 完成 root 私有 bootstrap、dry-run、apply 及 236 文件 overlay、保护配置、camd 和 EEG policy 核验；SSH 使用 `-T`，密码不进入命令、环境或文件。计划有效期十分钟，且执行前重查采集状态、privacy、锁、哈希及配置。默认使用 Family 绑定接口和地址，可通过 `--bind-interface`、`--bind-address` 显式覆盖。指定 `--sudo-command-file` 可导出无密码专家手工命令而不执行 root；该模式仍需人工核验执行结果。已完全安装 overlay 时核对 236 文件、EEG policy 和源码后跳过 root 变更。五文件混杂、未知哈希、其他 231 文件变化、设备身份或 profile shadow 不符均拒绝，不能用 `--approve-unknown-baseline` 绕过。stable 路由也拒绝覆盖已登记 overlay，含使用旧的 unknown 例外时。

仅注册 CLI，不改变 Web/broker 的版本选择。部署后仍需按现场要求单独检查 HTTP dashboard、日志和 EMG；不自动测阻抗或录制，软件测试不是硬件验收。交易脚本原样保存在 `resources/eeg-impedance-advisory-36d8059918ce/`，release 说明含防止静默降级的约束。

Build the reviewed 236-file source bundle from Git objects (never the dirty worktree):

```sh
python3 tools/fleet_maintenance/fleet.py --offline-plan
```

The offline build also emits a tar archive containing the five pinned USB component files and a SHA-256 manifest.

From a terminal, obtain and review an individual read-only device plan (substitute the confirmed IP):

```sh
python3 tools/fleet_maintenance/fleet.py --targets cp0014 --address cp0014=CONFIRMED_IP --ssh-config "$HOME/headring-qc/ssh/config"
```

Review every `diff`, protected hash, active-code shadow finding and component state; the plan is saved under the state directory with its SHA-256. Existing source must be absent, identical, or match the pinned commit's Git history. To plan a reviewed exception, use `--approve-unknown-baseline`; it is also required when applying that plan. This exception may overwrite user modifications, so review each one before approving. A plan is a local snapshot, not an approval from the device owner.

Apply the reviewed plan on the **same terminal host**, substituting the plan hash and address:

```sh
python3 tools/fleet_maintenance/fleet.py --targets cp0014 --address cp0014=CONFIRMED_IP --apply --accepted-plan PLAN_SHA256
```

`--verify --accepted-plan PLAN_SHA256` independently checks code hashes, protected hashes, controller state and camd PID. `--bind-interface` and `--bind-address` are optional SSH settings. SSH uses the configured user/known-hosts and strict host-key checking; an IP override retains the original configured host-key identity. Interactive root operations require the system `python3-pexpect` package and a local terminal: the program runs SSH with `-T` (no remote PTY), and uses `getpass` locally for at most one SSH and one sudo prompt. Passwords are not passed through arguments, environment, files or logs. A disconnected or changed device fails closed. Source and protected state are checked again before execution; the root transaction performs further checks, takes backups, holds capture locks during mutation, and supports selective rollback.

`--sudo-command-file PATH` is an expert, manually reviewed handoff for a non-terminal caller. The generated `-T` argv uses `sudo -S` and requires a local PTY credential helper; do **not** simply execute it as ordinary SSH or put a password in a command. There is no automatic executor for untrusted command files. Dry-run and apply are separate and the device preflight expires after ten minutes.

Apply runs the code transaction and the five USB component transaction in the same interactive sudo invocation. The root component transaction checks the accepted plan, drop-ins, device idle state and capture locks, backs up replaced files, and verifies the timer and component hashes; failures attempt selective rollback without unmounting devices. If an existing component is unreadable during unprivileged planning, root will accept that unknown state only when the component is already exactly current; a differing unreadable component requires separate manual review. No attached USB means software verification only, not a hardware mount test. A failed component stage may follow a successful code stage; inspect its reported root input/receipt before retrying.

Exit **0** means a plan was created or code and component checks passed; exit **3** means code checks passed but full component verification was not established; exit **2** denotes CLI/transport/safety failure. Commit=30, resume and unattended sudo bootstrap are not implemented. Do not run the copied historical `resources/preflight.py` directly; it retains old host-specific command-line settings. `component_files.py` is a sandbox primitive.

For a read-only metadata audit, substitute a confirmed device address and optionally provide a local candidate root (no recursive PC volume scan):

```sh
python3 tools/fleet_maintenance/fleet.py --audit-recordings --targets cp0018 --address cp0018=CONFIRMED_IP --local-root /path/to/raw-backups
```

The audit does not build or deploy a package. It reads bounded metadata beneath configuration-derived device storage and the two known cp0018 test roots, plus three known receiver databases in immutable mode. The latter may omit uncheckpointed WAL data. Local matching checks only explicit candidate layouts and native-summary hashes: even `metadata_only` is **not proof of a complete raw backup**. Unknown state or inaccessible paths remain unknown; videos are not hashed or transferred.

The addresses above were verified on the Family network at the recorded acceptance time; verify identity and address again when the network changes. The main controller deployed and verified cp0014 and cp0018 with this transaction using a manually adapted local-PTY/no-remote-PTY SSH handoff. Both reported 236 code files, all-ready software, unchanged 65-entry protected snapshots, idle controller and unchanged camd PID. On cp0014 no USB was attached, so hardware mount was not verified. Evidence: `/tmp/opencode/8788-main-cp14-cp18-deployed-verified.json` and the corresponding main apply logs. Existing cp0018 physical camera issues are not claimed to be fixed. This does not establish a complete raw backup.

Run tests: `python3 -m unittest discover -s tools/fleet_maintenance/tests -v`. The new built-in interactive transport is covered by mocked SSH/pexpect tests, not yet independently exercised against hardware; the earlier real deployments used the main controller's manually adapted handoff.

## 本机只读界面

在仓库根目录运行 `python3 tools/fleet_maintenance/web.py`，打开终端打印的 `http://127.0.0.1:端口/`。只监听本机，自动选择端口；按 Ctrl-C 只停止此界面，不停止采集或其他服务。启动不会扫描设备；只有点击设备的“刷新”才发起一次只读 SSH。页面定时请求的只是本机缓存。每个用户只允许一个界面进程。

配置在 `~/.config/headring-fleet-maintenance/config.json`（可编辑设备地址和本机网络绑定，不修改系统 SSH 配置）；缓存和进程锁在 `~/.local/state/headring-fleet-maintenance/`，目录 0700、文件 0600。使用已有 SSH 配置和严格 known_hosts，覆盖 IP 时保留原主机密钥身份。远端 DEVICE_ID 不符时保留旧缓存并明确标记身份不匹配；离线缓存不是实时数据。Family 网络地址是上次实测结果，并非永久身份绑定。

“今日”来自设备 API，日期取设备时钟；历史仅统计可读 profile 的 `daily_collection/records.json` 缓存，覆盖和账本更新时间必须一起看，非全量历史。已保存按 `saved_recording` 语义计算，与合格数不同；时长是账本缓存值。存储来自实际挂载卷的 statvfs，去重设备号，不是采集数据量。页面显示设备与本机时钟偏差但不校时。参考图通过只读控制器图库 API 分页按图片 ID 去重统计，显示可见范围和部分覆盖原因；控制器可能排除隐藏或导入中内容，待 QC 不从未完成数推算。原始字节、完整备份、云端签收尚无权威实现，显示未知；无硬件验收不能视模式通过。

设备详情中的深度只读检查读取 236 文件哈希并对照固定清单，显示保护条目数量及聚合哈希，不展示配置内容或单个受保护路径；这不验证组件硬件或与部署前快照一致。检查会短暂试探采集锁，不运行 CLI --verify、sudo 或写远端。界面可显式生成远端只读审核计划；已有界面元数据审计，但未完成完整校准验收；请审核计划。`--verify` 包含组件暂存及 sudo，不是只读。浏览器不接受密码，受控 apply 需要下述本机终端授权；完整独立验证仍需终端，commit=30 不支持，新 getpass/pexpect 模式尚未独立真机验收。

如需保留之前的地址，可运行 `python3 tools/fleet_maintenance/web.py 40177`；端口被占用时程序会报错，不会终止占用者。存储列表仅统计 mountinfo 中真实设备卷，按设备号合并并保留挂载别名。cp_0018 的账本零值不包括非标准目录中的测试历史。


## 工作台 Phase 1（部分实现）

启动 `python3 tools/fleet_maintenance/web.py 40177`；浏览 `http://127.0.0.1:40177/`。展开活动模式图库，按“加载本页缩略图”后可查看六张；“获取原图”完成后再次点击可打开原图。读取前检查设备身份、模式、空闲和隐私门禁，原图按 catalog 指定路径只读，拒绝符号链接和路径越界并校验哈希；资源缓存位于用户私有 state 目录。图库按同 revision catalog 的任务列表 ID、名称/ID 搜索及分页；记录仅以 catalog 精确项目相对路径或资产 ID 关联。非活动模式提供离线 catalog 的有限原图读取，完成状态未经验证。EEG catalog 缺失标为 absent，不创建目录或图库。

展开设备详情可查看两个 profile 的解析和配置 SD 挂载/UUID/数据根目录，并可“生成审核计划”。计划使用固定 pin、Git 对象、只读 preflight、安全门禁和现有 `prepare_plan/write_plan`；计划与包保存在 `~/.local/state/headring-fleet-maintenance/`，界面仅显示脱敏摘要。特权完整验证须在终端执行；终端 CLI 如需使用同一计划需用 `--state-dir "$HOME/.local/state/headring-fleet-maintenance"` 指向同一计划目录；参照本 README 上方 CLI 示例补齐目标 IP、网络绑定、SSH 配置和 accepted-plan 哈希。`--verify` 不是只读。当前支持单设备、终端授权的 Web apply；不支持任务 ZIP 导入或浏览器密码字段。

实机只读验收（将主动刷新所列设备、下载各三张任务图缩略图与原图并生成审核计划）：

```sh
python3 tools/fleet_maintenance/acceptance.py --base-url http://127.0.0.1:40177 --devices cp0014 cp0018 --readonly
```

现阶段尚无按设备并行任务、配置及状态目录参数、完整图片对抗测试；未连接与 UUID 不可读不能可靠区分，标为未知；界面仍有上述覆盖限制。图片读取仅在新任务时检测隐私状态；已缓存本机文件不会被远程状态自动删除。设备时间偏差仅提醒，不校时。

设备与图库、存储、历史可通过标签切换。历史中的“只读元数据审计”主动运行有界目录审计，cp_0018 的四段非标准测试单列，不纳入采集账本总数。元数据不是完整备份凭证。任务过滤使用 catalog 的实际列表 ID；部分/隐藏列表和活动控制器缓存的覆盖范围仍须注意。


## Phase 2A：终端解锁的单设备审核升级

先在界面显式刷新目标、生成并审核该设备计划及组件状态。计划必须在 10 分钟内、地址与当前本机 overlay 一致。保持界面运行，在**本机交互终端**执行（目标可改为 cp0018，或列出两个已各自生成计划的目标）：

```sh
python3 tools/fleet_maintenance/broker.py --state-dir "$HOME/.local/state/headring-fleet-maintenance" --targets cp0014 --ttl 600
```

终端通过 getpass 读取远端 sudo 密码，仅保留在 broker 进程内存，最多 600 秒；不支持 SSH 登录密码，须先配置已信任的 SSH 密钥。broker 固定目标、审核版本和启动时的计划哈希，通过用户私有 Unix socket 和同 UID 校验通信。Ctrl-C 在运行中事务结束后停止 broker；内存清理为尽力而为，Python 不保证绝对擦除。界面不接收密码。成功解锁后，在设备详情查看计划摘要，点击“确认升级”，阅读重启提示并输入准确设备 ID。操作前会重新读取并匹配预检、安全门禁、文件和保护状态，然后由既有事务做变更；失败可能在代码已写入后发生，`partial_failed` 或 `interrupted_reconcile` 必须人工核查远端回执，不能自动重试。组件与代码分别核验，但不代表 USB 硬件测试。当前没有批次/金丝雀队列、排队取消或手动回滚按钮；远端事务失败时仅有原安装器自身的选择性自动回滚，不保证全部恢复。不要依赖过期计划。

本版新增 broker 的成功和失败链路仅经过本机 Unix socket 与假 SSH / mock 测试，**尚未独立真机认证验收**；真实升级需要设备负责人自行决定并监控。测试与开发过程中不对真实设备执行 apply。任务 ZIP 导入属于后续阶段。先前证据和硬件状态不代表此次新 broker 已通过真机验证。

## Phase 2B：当前活动模式的 USB 任务导入

当前来源**仅为头环上的 USB ZIP**；尚不支持上传本机 ZIP，不会提供虚假的上传或非活动模式导入按钮。启动界面后进入“任务导入”，确认目标设备及当前 EMG/EEG 模式。点击“扫描设备 USB”会调用设备现有的 USB helper，**可能触发只读挂载**；它不是只读 GET，也不会开始导入。选择扫描结果，点击“预览选中任务包”：预览只读 ZIP 目录和内容，检查文件名、CRC、图像解码、像素、ZIP/展开大小并计算内容 SHA-256；读取同 revision 图库计算新增/复用/同名冲突、旧列表与空间估算。只接受控制器扫描得到的 ZIP ID，不接收网页提供的远端路径。

审核预览后，在本机终端为导入单独授权（不要求 sudo 密码，必须输入 `UNLOCK`；旧的升级授权不会授权导入）：

```sh
python3 tools/fleet_maintenance/broker.py --state-dir "$HOME/.local/state/headring-fleet-maintenance" --targets cp0014 --operations import_tasks --ttl 600
```

回到网页确认当前活动模式、旧列表保留、计划哈希并输入设备 ID，再提交导入。broker 只接受固定计划及设备，重新校验扫描身份、只读挂载、源 ZIP 哈希、模式、图库 revision、空闲/隐私状态、空间和保护快照；通过后仅调用设备控制器已有的 `usb_task_import` 事务，**不切换模式，不停止服务，不提供删除旧列表选项**。状态来自控制器提交过程；只有完成后校验新列表关联、原始图像 SHA、旧图库条目以及受保护文件/账本聚合未变才报告成功。无法确认结果标记 `interrupted_reconcile` 或 `partial_failed`，不要重复提交旧计划；导入计划一旦发出即在本机持久标记，必须人工核对控制器状态。扫码、计划和授权均有时效；同一时刻仅一个工作任务。

本版 ZIP/控制器事务链路在临时 ZIP、假控制器、隔离 Unix socket/假 SSH 下测试；**尚未对真实 USB 包进行端到端导入验收**。非活动 profile 不支持导入；无法读取或未初始化的图库会失败而非自动创建。预览计算的空间是保守估算，不是硬件健康证明。受保护快照覆盖 used.yaml、标定、设备身份、绑定和部分配置/凭据哈希，并非整机所有文件的证明。保持 ZIP 原件，元数据不代表完整采集备份。
