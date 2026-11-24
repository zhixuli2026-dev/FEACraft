# CLI 回合恢复与原生库存扫描进展

## 2026-11-21，补回已完成的原生回答

**FF-F41 已验收。** 非特权 WSL 搭配认证 SeaweedFS 时，首次打开会话的 Edge 自动进入 Codex · WSL 面板。用户点击“恢复会话”后收到 HTTP 200，原回答写入聊天记录。刷新后只有一条补回的回答，页面发起一次恢复请求，模型发送请求为零。API 随后在同一线程完成真实续聊。浏览器首轮驱动误操作已锁定的下拉框，修正为等待界面自动切换。SQLite、PostgreSQL、Windows、WSL、认证 S3 和原生终态前中断的证据见汇总。 [本轮证据](../../tmp/FF-F41-wsl-answer-20261121/validation.json) · [Edge 画面](../../tmp/live-cli-turn-recovery-e1f_00fn/browser-restored-answer.png)

本轮通过的 WSL 场景有 7 个完成的真实 DeepSeek 回合，以及 1 个收到实际模型正文后被中断的回合。首次浏览器驱动失败另有 1 个完成的真实回合。模拟调用 0 次，CLI 内部 HTTP 请求次数未逐次观测。部署沿用既有授权的 DeepSeek 配置和本机非特权 WSL 环境。恢复沿用现有 API 和数据结构，回退时撤销回答补写逻辑即可。此前出现的 WSL 启动过载仍归 N4-05 资源预算。

旧版在实际的非特权 WSL Codex 回合中收到真实 DeepSeek 回答，CLI 已关闭，原 API 在聊天事件入库前退出。原恢复接口把回合标为中断，聊天记录缺少回答。 [旧版回执](../../tmp/live-cli-turn-recovery-gsfkwxmb/live-result.json)

新版沿用原会话恢复接口。它核验原 API 退出、CLI 会话已正常关闭、原线程的最后一次原生尝试已完成，再从该次尝试读取已保存并脱敏的完成消息。正文读取在写事务外进行。提交时复核原回合版本、聊天中的用户消息、交付权限和存储收尾证明，随后同一事务写入回答、回合终态及恢复回执。未收到完整终态的输出保留在运行记录中。恢复操作沿用原回合，发起下一条消息时可选继续原线程。 [恢复实现](../server/app/services/codex_turn_recovery.py) · [真实验收入口](../server/live_llm_cli_turn_recovery_acceptance.py)

Windows 原生 CLI 搭配 SQLite、PostgreSQL 均通过实际 API 退出、双 API 并发恢复、跨用户拒绝与原线程续聊。两套数据库各完成两轮真实 DeepSeek 模型回合，旧版对照另完成两轮。CLI 内部 HTTP 次数未逐次观测，模拟调用为 0。Edge 通过未改动的聊天界面读回原回答和续聊回答，刷新后原回答只出现一次，网页未发送新的模型回合。 [SQLite](../../tmp/live-cli-turn-recovery-8qo3mrt1/live-result.json) · [PostgreSQL](../../tmp/live-cli-turn-recovery-hvarc3kh/live-result.json) · [Edge](../../tmp/FF-F41-browser-answer-p0z6m7b5/browser-result.json)

实际 Windows CLI 在原生完整终态前被中断的场景返回 409，随后只读核对确认聊天里只有原用户消息，部分文本仍在原生事件中。该轮整体验收保留失败，因为通用脚本原先要求自动恢复。当轮 WSL 单独复跑在启动存储盘点超时后返回 502，尚未进入模型回合。并行复跑的查询超时和另一轮启动范围错误一并保留。 [当轮证据](../../tmp/FF-F41-answer-recovery-20261121/validation.json)

后续在原 45 秒启动期限下，非特权 WSL 分别通过 SQLite、PostgreSQL 的实际退出、恢复和原线程续聊。两库各完成两轮真实 DeepSeek。另一次 SQLite 实测在已收正文但缺少原生终态时中断，恢复只登记中断，下一条任务可正常完成。独立盘点的 5 次实测为 1.09–1.23 秒。此前的启动超时仍作为 N4-05 的资源预算证据。 [WSL SQLite](../../tmp/live-cli-turn-recovery-_x8rmw84/live-result.json) · [WSL PostgreSQL](../../tmp/live-cli-turn-recovery-0eu2lb4k/live-result.json) · [原生终态前中断](../../tmp/live-cli-turn-recovery-x0t4dwg2/live-result.json) · [盘点计时](../../tmp/WSL-inventory-timing-20261121/baseline.json)

## 2026-11-21，CLI 启动的节点盘点

原生 CLI 启动先申请私有环境使用的 64 KiB 节点额度，完成实际主机及 WSL 盘点后进入账户和算例写事务。额度覆盖私有配置写入及事务提交，取消仍等待真实写入结束后释放。现有账户配额检查、启动期限和原生范围证明继续生效。 [启动实现](../server/app/services/codex_sessions.py) · [节点额度](../server/app/services/node_storage_budget.py)

旧版 SQLite 在节点盘点暂停时，独立写事务约 735 毫秒后因锁等待失败。新版的同一独立写入约 32 毫秒成功。PostgreSQL 在盘点暂停时可由另一连接取得同一账户的 advisory lock 并提交，耗时约 547 毫秒，含新连接。SQLite 搭配认证 SeaweedFS 的独立写入约 16 毫秒。三个新版用例均继续完成真实非特权 WSL CLI 初始化与关闭。Windows 另通过 8 个普通启动和 2 个持久启动检查，真实 DeepSeek 用两次 HTTP 200 完成验证文件的工具回读，模拟 0 次。 [资源复验](../../tmp/N4-05-cli-startup-lock-20261121/validation.json)

## 2026-11-21，CLI 启动的账户盘点

Windows 私有配置写入和 WSL 原生初始化额度共用账户用量版本。启动在写事务前读取实际用量，取得账户锁后按版本核对。并发变更使旧盘点失效时，在写入私有文件或登记额度前重新读取，沿用原启动期限。S3 可以直接复用发放额度时的已核验盘点，版本变化后仍需重读。锁内依据当前套餐和各类未结预留决定额度，已确认关闭的旧初始化额度照常结清。 [CLI 启动](../server/app/services/codex_sessions.py) · [账户配额](../server/app/services/cli_user_capacity.py) · [S3 额度](../server/app/services/owner_storage_budget.py)

旧版 SQLite 在启动盘点暂停时，同账户另一个算例保存返回 409。新版的 SQLite 私有配置阶段、PostgreSQL 原生额度阶段及 SQLite 搭配认证 SeaweedFS 均可保存并回读 192 KiB 正文，启动复核时计入实际增量，再完成非特权 WSL CLI 启动和关闭。1 MiB 配额专项中，另一个算例保存 480 KiB 正文后，实际账户用量达到 994,539 B，启动申请 64 KiB 时返回 429。实际没有创建私有会话，节点额度已清理。首轮测试正文触发了保存自身的额度拒绝，该轮保留失败，意外启动的隔离会话已通过正式恢复接口核验范围关闭和额度结算。 [账户盘点复验](../../tmp/N4-05-cli-account-admission-20261121/validation.json)

Windows 的 8 个普通启动与 2 个持久启动检查通过。认证 SeaweedFS 搭配非特权 WSL 完成了两轮真实 DeepSeek，包括 API 实际退出后补回原回答及原线程续聊。模型模拟 0 次，CLI 内部 HTTP 请求未逐次计数。独立 PostgreSQL 已删除，测试 S3 与隔离 API 的退出已经核验。 [启动检查](../../tmp/cli-account-windows-r4_8ngu3/cli-startup-checks.json) · [真实续聊](../../tmp/live-cli-turn-recovery-wdkqrvdg/live-result.json)

### 历史记录

2026-11-08。继续执行 AGENTS.md 的真实模型要求：模型文本、流、工具选择与 Agent 回合均使用授权的 `api.deepseek.com / deepseek-flash`。基础设施检查单独计数，不能替代真实模型验收。

当前结果：SQLite、PostgreSQL 各 4 个退出边界已通过；合计 8 个独立恢复场景、8 个恢复后完成的真实 CLI 模型回合。库存改为固定的 WSL 元数据助手，原有 12 项实际检查及新增 6 项检查通过，并有 4 次真实平台 Agent HTTP 请求完成工具回读。剩余范围见末节，未据此关闭 N4-05 或整项交付目标。

## 回合恢复实现与保留的失败

新增 `POST /api/v1/codex/turns/{id}:reconcile`，在实际核验原 API 进程身份已退出、会话原生范围已关闭后，提交回合恢复凭据。非终态回合标为 interrupted，保留原 native_status、模型尝试与历史事件，过期未完成审批，不重放模型或命令。执行锁仅在同一会话的其他原生资源也安全时释放；staging 不会凭空标为已恢复。

实现见 `app/services/codex_turn_recovery.py`；显式验收入口为 `live_llm_cli_turn_recovery_acceptance.py`。验收先调用真实模型，再在实际事件提交边界暂停并终止 API，重启独立进程恢复，最后必须在同一会话完成新的真实模型回合。只看到 idle 锁不算整体验收成功。

- `tmp/live-cli-turn-recovery-q9oo16bs`：实际模型终态后的恢复已提交，但首次与重复响应比较失败；该次失败保留。之后统一从数据库刷新时间字段，再次验收重复响应一致。
- `tmp/live-cli-turn-recovery-ivvax2hk`：重复恢复响应一致，但恢复后新 session 启动返回 502，因此整体失败。同目录的后续启动诊断均未调用模型，`real_model_requests=0`；诊断重用了此隔离库并更新了 fixture/API 日志，原 `live-result.json` 保留。
- `tmp/live-cli-turn-recovery-wxa9xjwf`：真实 DeepSeek 返回文本并报告 8,696 tokens；两个独立 API 进程恢复结果一致，重复请求一致。去除重复 lstat 后，后续 session 启动仍超时，整体验收失败，不能记为功能完成。
- `tmp/live-cli-turn-recovery-xupwjxo_`：进一步优化扫描后，首次真实 CLI 回合报告 8,704 tokens，独立恢复和重复响应一致；恢复后的 private_initialize 降至约 14.7 秒，但后续完整启动仍返回 502，第二次模型回合没有开始。初始化步骤完成不等于 session 启动成功；本次仍是失败证据。下一步沿实际 start/activate/初始化结算步骤分解剩余时间，不能只看 prepare 日志判断成功。

CLI 内部 HTTP 请求没有逐次计数；一个 CLI 回合不等于一次上游 HTTP 请求。以上均未使用模型替身。

## 实测启动瓶颈与修复

`ivvax2hk/restart-diagnosis-timed.json` 为一次实际启动失败，模型请求为 0。计时记录显示：用户库存扫描约 22.5 秒、节点库存扫描约 21.1 秒，private_initialize 共约 43.6 秒，挤占 45 秒启动期限。scope allocation 取消的异常栈是超时表现，不能据此断言范围控制器本身是根因。

同目录 `native-scan-profile.json` 对保留的真实 WSL 目录进行只读剖析：7,942 条目、87,872,857 字节；扫描约 23.2 秒，20,739 次 lstat，其中 nt.stat 累计约 19.1 秒。

`cli_native_usage.py` 改为消费 `os.scandir` 返回的 no-follow 文件属性，普通文件与链接不再逐个重复发起 UNC 查询。目录仍使用新的 lstat 获取真实 inode，并保留遍历前后身份校验；根目录重定向、其他 reparse 类型、特殊文件、期限和条目上限继续拒绝。共享的 `_ordinary_info` 只分离属性验证，原 `_ordinary(path)` 仍执行实际 lstat。

`native-scan-after.json` 的相同目录扫描约 10.8 秒，字节数与条目数完全一致。这是一次实际对照，不代表稳定吞吐量，也不代表库存是并发写入下的原子快照。没有提高启动期限、放宽额度或跳过库存检查。

修复后的 `tmp/live-cli-startup-8jaksqz9/cli-native-usage-checks.json` 已通过 12 项实际检查，包括安装的 WSL CLI 初始化与核验退出、两个用户的字节归属、节点和用户超额拒绝、原生 vault、目录及悬空链接只计自身字节、根目录缺失和根目录重定向拒绝、条目上限及运行中超额关闭。随后平台 Agent 的真实文件工具回读成功：2 次 DeepSeek HTTP 200，报告 8,556 tokens，模型回复与实际文件一致；隔离 API 已退出。该平台 Agent 证据不冒充 CLI 自身推理。

## 当前库存实现与恢复验收

四线程目录枚举曾使同一 7,942 条目目录降到约 4.8 秒；实际条目上限、容量下限提前停止和期限耗尽均确认没有遗留工作线程。`tmp/live-cli-turn-recovery-tkk69q4p` 首次完整通过 native_terminal 边界：两独立 API 恢复后，同一会话的新真实模型回合 completed、回复入库且关闭已核验，新回合报告 8,800 tokens。

随后连续四边界测试 `tmp/live-cli-turn-recovery-fyn8n2p8` 只有 text 边界完整通过；真实待审批回合的恢复和审批过期已提交，但新 session 再次超时。实际目录枚举发现插件临时克隆残留使库存增至 15,714 条目、197,628,471 字节。未删除残留，未将这次连续验收记为成功。增加到 8/16 个线程仍需约 7.7/5.2 秒，不能据单次成功证明问题解决。

当前正式扫描改为 `cli_native_inventory.py` 和固定的 `cli_native_inventory_reader.py`：宿主采用现有 OwnedProcess Windows Job 启动固定助手；Linux 部分以 nobody 身份只读目录元数据，不执行任意命令、不读取用户文件内容或创建子进程。输入、输出、条目、剩余容量和期限均有界；原生部分使用 11 秒内核 SIGALRM 与 owner-pipe EOF 退出，宿主继承调用方剩余期限并排空已拥有的进程。成功返回前核验宿主范围关闭、原生助手的精确出生身份已退出、助手源码哈希及根 inode；未知、超额或失效结果拒绝准入。每次核验仍重新枚举实际库存，不缓存结果或省略启动准入检查。

该助手在上述真实残留目录得到完全相同的条目与字节数，含启动和退出核验约 4.1 秒；这是一次观测，不能宣称所有规模和并发均满足启动期限。原有 Windows 多线程枚举已从正式路径移除。

`tmp/live-cli-startup-0x0rpyrw` 已以当前助手通过原有 12 项真实库存、链接、额度和运行中超额关闭检查；随后平台 Agent 实际文件回读通过，2 次 DeepSeek HTTP 200，共报告 8,562 tokens。

`tmp/live-cli-turn-recovery-6uac04bz` 已通过 SQLite 连续四边界：text、真实待审批、native_terminal、session_closed。每项都有实际 API 退出、两独立 API 恢复结果一致、重复恢复一致，以及同一会话的新真实模型 completed 回合；原始事件和 native_status 保留，不新增旧回合的助手成功消息，未决审批过期。4 个新模型回合的回复入库及进程关闭均已核验，主 API 和 4 个独立对账 API 均退出。这些是 CLI 自身的真实 DeepSeek 推理；CLI 内部 HTTP 请求数仍未逐次观测。

原生助手异常退出、API 中断和硬生存期限的专门故障覆盖仍需补齐，不能将普通成功退出代替这些证据。PostgreSQL 四边界及新回合完成后的旧恢复幂等性另行记录；不能用 SQLite 的结果代替。

`tmp/live-cli-turn-recovery-edsumu3l` 已独立通过 PostgreSQL 的同样 4 个边界，每项还在新真实模型回合结束后重新请求旧回合恢复，响应与初次恢复一致。4 个独立对账 API 均退出，`tmp/cli-turn-recovery-postgres-ez2adqxu/result.json` 核验临时数据库已删除，开发库未修改。

SQLite 的新回合后幂等性另经实际 API 验证：`tmp/live-cli-turn-recovery-6uac04bz/post-followup-replay.json` 的 4 项均通过，模型调用为 0。此补充检查重用了真实模型测试库，原 API 日志保存为 `api-before-post-followup-replay.log`，`api.log` 和 `result.json` 更新为补充检查的独立 API，原 `live-result.json`、模型事件和四次原 owner 日志保持不变。

`tmp/live-cli-startup-m9i30vbe/native-inventory-faults.json` 新增 5 项实际基础设施检查（普通文件和悬空链接计数、共享计数器容量下限、条目上限、实际 FIFO 拒绝、原生内部期限结果及精确进程退出核验）和 1 项纯协议检查（助手源码哈希变更拒绝）。随后真实平台 Agent 读取证据文件成功，2 次 HTTP 200，共报告 8,552 tokens。它不等于硬信号期限、SIGSTOP、宿主死亡或未知原生出生的完整验收。

## 后续任务与交付计数

继续处理 S3 staging 保留及安全释放、startup/fork 恢复、恢复自身提交中断、活动新回合期间旧恢复不得影响新执行锁，以及错误凭据和历史格式拒绝。现有“新回合完成后幂等”不能代替“新回合仍在运行”的覆盖。真实审批回复/拒绝/取消、Windows CLI 模型回合、fork/resume 及库存助手异常生存期仍待；本阶段只验证失联审批过期，不宣称审批交互已完整实现。

主线 33/53 完成，3 项进行中、17 项待办，剩 20 项；资源 11/17，剩 6 项。全功能 295 项仍待整项交付验收。新增实现及局部证据不改变这些完成数。
