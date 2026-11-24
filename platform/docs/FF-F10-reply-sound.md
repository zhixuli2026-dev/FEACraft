# FF-F10 Agent 回复提示音

原审计已有 WebAudio 提示音，待对齐开关默认值与触发条件。桌面程序分别保存 `agent/replyNotificationSound` 和 `agent/replyNotificationIcon`。声音默认关闭。回复完成时先判断声音开关并调用播放器，随后独立判断窗口焦点与任务栏图标提醒。已复核安装版 `VibeFlowStudio.exe` 的偏好读取、完成信号和媒体调用地址。 [功能差距审计](feature-gap-audit.md#f-1-对话模型与展示) · [原始二进制复核](../../tmp/f10-original-reply-sound-review-20261119.json)

工作台在 API Agent、Windows Codex 和 WSL Codex 的「会话设置与历史操作」中共用「回复提示音」开关，浏览器按当前账户记住选择。开启开关或下一次提交时，浏览器可通过实际用户操作授权播放。API Agent 收到带已提交答案 ID 的成功终态时播放一次。原生 Codex 按本页面发起的消息 ID、所属会话和真实回合核对执行记录，收到 `stream.done` 且 `native_status=completed`、`cleanup_verified=true` 后播放。切换会话时仍可等到已发送任务的提示，取消、请求失败和历史读取保持静默。 [工作台开关](../web/src/panels/ChatPanel.tsx) · [原生入口](../web/src/panels/NativeChatPanel.tsx) · [共享声音及原生回合观察](../web/src/utils/replySound.ts)

## 实际验收

正式入口以隔离 SQLite 和本地文件存储运行工作台，在 Edge 保留实际 WebAudio destination 并旁路读取其同一音频图的采样。验收本身统计实际官方 DeepSeek 请求，追踪持久终态、运行所有权释放和隔离进程退出。 [显式实际模型验收入口](../server/live_llm_reply_sound_acceptance.py)

- 首轮实际发出 3 次 `deepseek-flash` 官方 HTTPS 请求，均收到 HTTP 200。启用后切换到另一会话，成功回合产生一次 220.59 毫秒的双音，44.1 kHz 实时采样测得 874.92 和 1320.60 Hz。两段各经一个实际输出增益节点。关闭后下一次成功回合保持静默，刷新后开关仍关闭。两个完整回复合计 6,404 tokens，上游正文摘要与隔离数据库中的回复一致。 [实际浏览器回执](../../tmp/live-f10-reply-sound-0ttcqa65/reply-sound-browser.json) · [实际音频样本](../../tmp/live-f10-reply-sound-0ttcqa65/audio-native-samples.json)
- 首轮第三次真实请求只在观测窗口内输出推理，夹具等正文超时。浏览器退出后真实中断，持久终态为 `interrupted`，上游未返回该回合的用量。隔离 API PID 15272 已退出，54889 已关闭，三条会话执行所有权均回到 `idle`。 [原始失败回执](../../tmp/live-f10-reply-sound-0ttcqa65/live-result.json) · [退出和持久状态复核](../../tmp/live-f10-reply-sound-0ttcqa65/independent-cleanup.json)
- 取消补验发出 1 次实际 `deepseek-flash` 官方请求并收到 HTTP 200。接到真实推理首片后，另一个标签页提交同一会话，收到 `409 state_conflict` 并保持静默。原标签页点「停止」，收到顺序一致的 `ready`、`reasoning.delta`、`run.cancelled`。数据库保存 `cancelled`，所有权回到 `idle`，两页都没有提示音样本。上游未返回该回合的用量。 [补验回执](../../tmp/live-f10-reply-sound-03_9urq_/live-result.json) · [实际取消事件](../../tmp/live-f10-reply-sound-03_9urq_/cancelled-wire.json)
- 补验从首轮的实际历史克隆，刷新当前工作台后仍能读取首轮真实回复并显示开启状态。刷新期间音频上下文创建次数为零。复验进程退出码 0，PID 29008 已退出，57475 已关闭。累计 4 次官方 HTTP 200，无模拟推理。 [设置和历史截图](../../tmp/live-f10-reply-sound-03_9urq_/reply-sound-settings.png) · [浏览器断言](../../tmp/live-f10-reply-sound-03_9urq_/reply-sound-browser.json)

既有真实上游中断证据覆盖 API Agent `stream.error upstream_incomplete` 的落账与历史恢复，信号发生时的音频采样未纳入该次记录。 [既有实际流中断事件](../../tmp/live-cli-startup-1he_jlc9/closed-before-finish-events.json)

## Windows 和 WSL 原生 Codex

Windows 首轮通过正式工作台发起原生用户回合，DeepSeek Responses 实际返回 HTTP 200，共 7,505 tokens。切换到另一会话后，Edge 读取实际双音输出 208.98 毫秒、874.92 和 1320.60 Hz，各经一个增益节点。夹具随后在已加载历史同时匹配到用户提示与助手答案，原回执保留失败。独立读取原数据库确认助手正文已提交，`stream.done` 已持久化，原生范围为空，API PID 35480 已退出且 59091 已关闭。该轮 WSL 模型请求数为零。 [首轮回执](../../tmp/live-f10-native-reply-sound-61aa22xm/native-reply-sound-result.json) · [Windows 音频采样](../../tmp/live-f10-native-reply-sound-61aa22xm/audio-windows.json) · [Windows 独立完成核验](../../tmp/live-f10-native-reply-sound-61aa22xm/windows-completion-independent.json)

独立冷启动从真实 Windows 数据库备份重开历史，定位到唯一的助手答案，Edge 音频上下文创建次数为零。首次 WSL 补验在工作台能力自检并行期间初始化超时，管理进程身份和桥接已登记，随后实际收尾并验证范围为空，未提交原生推理回合。等普通能力查询结束后用工作台「检查运行环境」重新启动，完成初始化、关闭会话、验证范围为空。三个阶段保留各自的 API 和端口退出证据。 [Windows 历史复开](../../tmp/live-f10-native-reply-sound-o8ezq_p0/native-reply-sound-result.json) · [WSL 初始超时](../../tmp/live-f10-native-reply-sound-lnpwyswg/wsl-startup-timeout-independent.json) · [WSL 零模型预检](../../tmp/live-f10-native-reply-sound-lfzt3s1m/native-reply-sound-result.json)

WSL 正式验收等两次工作台能力查询都完成后，从正常页面发送原生任务，随后切换会话。`nobody` 身份的本地 Linux Codex 走真实 DeepSeek Responses，HTTP 200，共 6,376 tokens。Edge 实测 220.59 毫秒、874.92 和 1320.60 Hz，确认一次双音。持久 `stream.done` 表明原生成功与收尾已验证，助手答案已提交，真实原生范围为空。刷新后重新读取 Windows 与 WSL 助手答案，音频上下文创建次数为零。隔离程序退出码 0，API PID 34884 和 49521 端口已关闭，WSL 本地转发也已退出。两种原生后端共 2 次真实 HTTPS，13,881 tokens。 [WSL 实际验收](../../tmp/live-f10-native-reply-sound-3x6nkh4j/native-reply-sound-result.json) · [WSL 原生终态](../../tmp/live-f10-native-reply-sound-3x6nkh4j/terminal-wsl.json) · [真实音频采样](../../tmp/live-f10-native-reply-sound-3x6nkh4j/audio-wsl.json) · [Edge 设置截图](../../tmp/live-f10-native-reply-sound-3x6nkh4j/native-reply-sound.png)

原生失败与取消通过既有真实持久事件和当前播放条件核对。Windows 用户发起的真实取消结束为 `cancelled/interrupted`，实际被拒的原生模型结束为 `failed/failed`，两者都不满足播放条件。这两个原生负例的现场未采集音频。 [实际用户取消](../../tmp/live-native-goal-0fjbxnm3/native-goal-result.json) · [实际模型失败](../../tmp/live-cli-inference-myxstsj8/live-result.json) · [播放判定实现](../web/src/utils/replySound.ts)
