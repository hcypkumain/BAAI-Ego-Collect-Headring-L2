# v8.4 语音缓存与手机调整状态

此分支从 v8.3 继续保留选择性任务库导入实现。它修复了提示音音量反馈、脑电滤波草稿被轮询覆盖、缓存重复合成与损坏缓存重试等问题。没有把语音播放或设备设置变成采集前置条件。

## 每步播报与预生成

| 环节 | 语音策略与代码路径 | 预生成 | 本次验证与边界 |
| --- | --- | --- | --- |
| 启动、设备确认、缺失设备 | `control/launcher.py` 播报设备概况、错误指引和确认结果；设备名/接入状态由运行时拼接。 | 固定设备确认指引纳入清单；运行时设备概况仍按最终文字缓存。 | AST 覆盖测试检查固定文字；真实听感未测。 |
| 腕部左右标定 | `common/wrist_calibration.py:PROMPTS` 涵盖选择方式、手动确认、baseline、左右遮挡、恢复、通过、重试、设备变化、配对。 | 所有固定阶段提示纳入启动预热。 | `test_speech_prompts.py` 核对固定文本与阶段 ID；相机硬件未接入。 |
| 肌电左右与接入顺序 | `common/emg_calibration.py:PROMPTS` 和 `common/emg_insertion.py:SPEECH` 说明左右确认与逐个插入接收器。 | 所有固定阶段提示纳入启动预热。 | 固定文案覆盖测试通过；肌电接收器未连接。 |
| 参考图与准备 | `launcher.py:announce()` 使用 `speech_prompts.task_announcement()`，与选中参考图同一标题字符串。手动选择参考图时和 `reference_begin_preparation()` 实际抽图后都提前排入优先级较高的预热队列。 | 动态任务标题在正式播报前预取，命中有效 WAV 后直接播放。 | `test_speech_prompts.py` 验证标题生成与固定字面提示覆盖；真实音频未生成。 |
| 开始门控、设备未就绪 | `launcher.py:begin()` 在触控屏、准备状态、阻抗、相机检查未通过时播准确原因；不宣布已开始采集。 | 固定错误指引纳入预热。 | 代码路径与字面提示有自动覆盖；设备未实测。 |
| 倒计时 | `web_controls.py:countdown_voice()` 播放打包的本地 `char_5.wav` 至 `char_1.wav`，每条提示约 0.8 秒有效期；不在倒计时请求云合成。 | 本地 WAV 已随程序打包；不是由本次生成。 | 通过源码和既有控制测试检查；本次未从设备喇叭实听。 |
| 正式录制 | `launcher.py` 将 `voice.silent` 设为 true；不为覆盖屏幕提示而强行播报。 | 固定缓存保留在磁盘供后续提示使用。 | 录制静音门控以源码及当前代际实现为准；没有进行真实录制。 |
| STOP 收尾、确认保存、QC 与放弃 | `launcher.py:poll_stop()/finish()/poll()/discard_and_retry()` 分别播停止已确认、收尾失败、保存待质检、质检完成、失败保留、重采和清理结果。STOP 未确认时不会播保存成功。 | 固定文案和 QC 模式组合纳入清单；动态 QC 细节不做无界预生成。 | 已有 `test_delivery.py`、`test_stop_completion.py` 覆盖相关收尾顺序；本次未执行现场录制/QC。 |
| 重复提示与动态错误 | 当前步骤通过现有提示重播；旧 generation 的播放会被 interrupt 取消。未知 QC/故障文字按需排队，短 TTL 到期即跳过，采集继续。 | 已有缓存直接播放；同一文本用内容键去重，正在生成的文本单飞。 | `test_speech_cloud_cache.py` 覆盖缓存命中、同文本并发、失败轮转；`test_speech_preparation.py` 覆盖 interrupt 保留共享 cache 与预热队列。 |

预热集覆盖固定标定、设备确认、启动门控、保存/停止结果、基础错误指引和倒计时字。任务标题按最终 asset 名动态预取。动态错误、QC 详细行和设备列表不在离线固定集内；首次遇到且没有缓存时可能跳过一次，后台最多有限次重试，不阻塞后续提示。

Cloud cache key 包含文本、TTS 模型、音色、端点、语言、输出格式和 cache schema 版本。有效 WAV 必须是完整的单声道 16-bit PCM、24 kHz 文件。旧版本 cache 中仍有效的 WAV 会复用。损坏文件只在持有同 key 锁并复核后隔离；生成通过现有 `cloud_tts.py` 临时文件、fsync 和原子替换完成。每条失败有界重试并轮换到其他文字，重复请求不重复启动同一文本的合成。

`speech_tools.py test` 默认只查稳定测试句的缓存，不联网合成。明确请求合成时使用 `--force`。固定提示预热命令为：

```sh
/home/orangepi/Desktop/Embodied-Brain-Collect-main.2/orangepi/.venvs/framework/bin/python \
  /home/orangepi/Desktop/Embodied-Brain-Collect-main.2/orangepi/scripts/audio/speech_tools.py warmup --wait-seconds 300
```

状态清单写入 profile 的 `orangepi/runtime/speech_status.json`，按稳定 prompt ID 标记 ready、pending 或 error。当前工作区没有运行真实 warmup，也没有生成真实云端音频文件。

## 手机设备与媒体调整

| 控件 | 端到端代码路径与状态 | 行为验证 | 未验证项 |
| --- | --- | --- | --- |
| 头环提示音音量 | `dashboard.html` 手机设置页 → `/api/action volume` → `WebControls` → `Speaker` → `AudioRouter`，操作 Pi 的 Pulse sink。状态区分 requested、applied、sink、读回和失败；应用成功须 `pactl set-sink-volume` 后再 `get-sink-volume` 读回匹配。 | `test_audio_volume.py` 3项验证成功读回、同值失败不沿用旧成功、过期路由结果不覆盖新请求；`test_dashboard_settings.cjs` 验证草稿、失败、实例重启和超时。 | 没有实际改现场音量或播放。 |
| 腕部自动曝光/帧率策略 | `dashboard.html` 选择 → `wrist_camera_settings` → 受控设置 worker → V4L2 写入与读回 → 偏好保存；应用失败保留回滚结果。断线时输入和应用按钮禁用。 | `test_wrist_camera_settings.py`、`test_wrist_camera_dashboard.py` 和 `test_dashboard.py` 合计 71项通过；测试 fixture 已更新到真实 privacy、worker 和设置 worker 合同。 | 未接入相机做读回。 |
| 脑电滤波 | `dashboard.html` 选择 → `eeg_filter` → preset 校验 → `runtime/eeg_preferences.json` → `eeg_settings.apply` 更新本段配置。页面状态轮询保留未应用草稿，等待实际读回；结果不确认会释放 pending 并要求核对，不会永远锁按钮。 | `test_dashboard_settings.cjs` 覆盖选择保留、成功读回、失败保留、超时/重启；`test_eeg_control.py` 6项在独立 Python 3.12/NumPy 环境通过，含实际 dashboard action 写偏好。 | 未接 EEG 硬件验证滤波效果。 |
| 重扫、设备模式、左右标定 | 手机/控制页动作仍进入 revision、状态、privacy 与设备 owner 校验；录制中不提供设备配置操作。 | 当前基础 action/controller 自动测试通过；本次未改这些 routes。 | 物理设备重扫和左右确认未实测。 |
| 耳机搜索、连接、断开、遗忘、试听 | `phone_headphones.js` 调用 Pi-owned headphone sidecar；连接状态与音频通道分开显示。服务端忙、采集状态、陈旧状态会禁用对应操作。 | `test_phone_headphones.cjs`、`test_headphone_capture_gate.py` 13项覆盖状态和门控。 | 未配对/连接耳机；没有实际播放试听音。 |
| 参考图缩放/平移与任务预览 | `/phone` 内加减、双指平移/缩放、重置只改变当前预览。 | `phone_app.js` 已逐项检查事件绑定和局部缩放边界。 | 本次没有新增对应交互自动化或手机实机测试。 |
| 回看播放、时间轴、前后跳、速度、信号窗口、滤波 | `phone_app.js` 调用本地播放器/信号展示；速度、窗口、显示滤波调整回看视图，不改采集设备设置。 | 既有 replay/source 路径静态核对；`test_phone_result.cjs` 仅覆盖结果归属和保存回执。 | 未对全部触控和长录像回看逐个做端到端行为测试。 |
| 保持亮屏 | 手机浏览器 Wake Lock，权限失败走页面错误提示；它不修改 Pi 设备。 | 静态核对 `navigator.wakeLock` 获取/释放事件。 | 未覆盖各手机浏览器与电量策略。 |

## 自动回归与线上边界

- 本轮音频相关选定套件共 27 个 Python 行为用例全通过（包含已有 13 个耳机门控用例）；另有 1 组 dashboard EEG/音量草稿 JavaScript 行为测试通过。
- `test_wrist_camera_settings.py`、`test_wrist_camera_dashboard.py`、`test_dashboard.py` 合计 71 项通过；`test_eeg_control.py` 6项通过（Python 3.12，NumPy 测试依赖）。
- 一次组合运行中 `test_dashboard.PreviewTests.test_signal_preserves_signed_extrema_and_bounded_history` 报 145/150；该单测单独运行和完整 `test_dashboard` 重跑均通过。原因未确认，不能据此认定与本分支改动相关或无关。
- `test_v4.CloudTests` 中 1项真实 CloudCache worker 生命周期用 mock 子进程通过；没有请求云端。
- 已有耳机门控、耳机 UI、手机结果与 screen sync 测试通过。与现异步 advisory speaker 契约不一致的旧 `test_cloud_only.py` 和 `test_prompt_flow.py` 使用同步/fatal 语义，本次没有据此恢复阻塞式播报。
- cp_0019 只读状态曾显示 speech cloud generation 有 `TimeoutError`、待处理 cache 项和空 WAV cache；现场 source hash 与本轮修改前基线中的若干音频文件相同，但 `web_controls.py` 与当前 checkout hash 不同。`pactl` 默认 sink SUSPENDED 属于空闲状态，不能单独判故障；旧 `audio_output.json` 缺 policy 字段时按 legacy 规则处理，`enabled:false` 不足以说明语音关闭。cp_0018 SSH 不可达。
- 本分支没有部署。上述现场快照是之前代码的状态，修复尚未在任何真实设备运行；没有通过本次代码证明 cp_0019 的 TTS 已恢复正常。没有打印、读取或发送云 API 密钥，没有真实播放、改音量、运行云预热或启动采集。
