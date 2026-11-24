# N4-03.g2b2：原生回合、事件、审批与会话执行归属

2026-11-03。宿主 Codex CLI 已通过正式 HTTP API 接通持久回合、SSE、原生审批、计划问答和已核验线程的显式续接。API Agent、Goal、工具审批与 Codex 共用 conversation 执行归属。本文记录这一交付单元；完整主线 53 项及全功能 295 项仍全部保留，N4-03 不关闭。前序私有账户及两级进程登记见 [owned-cli-sessions.md](owned-cli-sessions.md)，所属通信基础见 [owned-codex-transport.md](owned-codex-transport.md)。

## 数据与准入

新增四张表，迁移 head `f97e54a8d310`，共 30 张业务表：

| 表 | 记录及约束 |
|---|---|
| `conversation_executions` | conversation 唯一执行 owner；backend、resource、私有 token 摘要、出生身份、租约与版本。idle 清空全部 owner 字段，active/recovery_required 保留完整归属 |
| `native_cli_turns` | 用户、case、conversation、CLI session、客户端消息 ID、请求摘要、模式、状态、原生 thread/turn ID、容量重试 checkpoints、事件计数及终态时间 |
| `native_cli_events` | 每回合单调 seq 的已提交公开事件；复合主键防止同一序号重复 |
| `native_cli_approvals` | 当前尝试、区分数字/字符串的服务端 request ID、方法、原请求摘要、公开参数、领取状态和公开答复 |

客户端消息 ID 按 conversation/user 唯一，绑定 content、goal、timeout、resume 的请求摘要。重复相同请求返回原记录，包括 CLI 已关闭或 owner 已退出的历史；相同 ID 不同请求返回幂等冲突。原生副作用不会通过历史读取或重试 POST 再次执行。启动尝试意图先提交，再发送 turn/start；真实原生 turn ID 先提交，再消费事件。登记失败立即退休自己实际持有的进程范围，不能据此重发原文。

准入在 case 协调边界内复验用户、case/conversation、权限及来源，领取唯一 conversation owner 并提交用户消息与回合。原生启动、模型等待和用户审批等待在锁外。5 秒续租，30 秒租约；租约过期或 owner 出生不再匹配时不自动接管、补造成功或重放审批。N5 仍需实现完整的恢复和人工对账流程。

API、Goal 和审批执行均使用同一归属；Goal 在多轮之间持有 owner。原生 shell 的启动门再次复验 conversation owner。API 启动恢复会保留出生仍匹配或未能证明退出的其他 worker，不因另一实例启动就把其运行中工具、run/request 历史改为 interrupted。真实 owner 退出后，旧 API 历史可标记 interrupted；这不代替原生停止证据。已失联的原生回合保持需要对账。历史接口跨过终态事务时，会刷新较旧的回合，避免把新提交的 idle fence 和旧运行状态拼成假异常。

## 正式 API 与事件契约

下表路径前缀均为 `/api/v1/codex`，会话仍由 `POST /sessions` 创建。原生回合要求明确绑定 case 与 conversation；HTTP 不能指定程序、环境、账户目录或任意历史 thread ID。

| 方法 | 路径 | 行为 |
|---|---|---|
| POST | `/sessions/{id}/turns` | `{content,client_message_id,goal?,timeout_seconds?,resume?}`；提交后返回持久回合，后台执行，停止观察不取消 |
| POST | `/sessions/{id}/turns:stream` | 同一准入后以前台 SSE 观察；新创建请求断线会取消并等待所属范围退出；幂等重连不会取消原 owner |
| GET | `/turns/{id}` | 当前用户回合、尝试 checkpoints、已提交最后 seq 与清理证明 |
| GET | `/turns/{id}/events?after=N` | 只读 SSE 重放及继续观察；支持 `Last-Event-ID`；断开观察不重新执行或取消回合 |
| POST | `/turns/{id}:cancel` | 本机 owner 取消并加入实际回合及 CLI 收尾；其他 worker 无权代发中断 |
| GET | `/turns/{id}/approvals` | 投影后的审批记录及状态，包含已经发送/过期记录 |
| POST | `/turns/{id}/approvals/{aid}:respond` | 当前 owner 对绑定的待审批请求恰好领取一次并发送 |
| POST | `/sessions/{id}/thread` | 支持 `{resume:true}`，仅续接当前用户/会话最近一次已完成且停止已核验的原生线程；默认创建新线程 |

SSE 使用 `id: seq`、`event: type` 与 `{type,turn_id,seq,payload}`。只发送数据库中已经提交的事件，游标不得为负或超过最后已提交序号。主要类型包含 turn.admitted、request.started、turn.started、text.delta、reasoning.summary.delta、item.started/completed、tool.output.delta、approval.requested/claimed/sent/resolved、request.completed、stream.done。原生 turn/completed 对应 request.completed；只有进程、暂存和数据库交付完成，才提交 stream.done。失联或提交未知时发送 execution.unavailable 并结束观察，不补造 stream.done。

每回合累计最多 64 MiB/20,000 个原始事件及已持久事件；单持久事件最多 1 MiB，终态预留空间。底层仍有 1 MiB RPC、256 个/4 MiB 待消费事件和 128 个服务端请求等独立上限，超限退休所属范围。单条用户文本最多 128 KiB，超时为 1–3600 秒；累计回答最多 8 MiB。工具输出保留其原始文本语义并可视化 NUL，模型文本经过协议标签清理。超出限制不会将截断内容伪装为已完成回答。

## 审批、计划模式与秘密输入

适配安装版本生成的协议，支持四类请求：命令、文件变更、权限申请、用户问答。命令/文件决策支持 accept、acceptForSession、decline、cancel，并验证当前可选决策。命令政策扩展只能采用原请求明确提出的 exec/network rule；权限答复只能是请求权限的子集，不能用普通 accept 代替权限 profile。授权范围限 turn/session。原生 request ID 的类型和当前尝试共同绑定审批，不将字符串 `"1"` 和数字 `1` 混同。

问答 HTTP 格式为 `{"answers":{"question-id":["回答"]}}`，服务端转换成 CLI 的嵌套 answers 对象。必须回答当前全部问题 ID；普通选项题拒绝未提供的标签，自由答案由 isOther/isSecret 控制。问答答复、权限答复整体最多 64 KiB。重复/并发答复只有一次能领取发送；发送结果未知时保留未知状态、停止回合，不重复发送。

plan 会传入实际 `collaborationMode.mode=plan` 和 CLI 解析出的 model，真实 CLI 因而能调用原生 request_user_input；只修改权限标签不足以启用该工具。ask/plan 均为只读，不允许通过命令或权限审批升级；用户问答及明确拒绝仍可操作。所选 conversation model 固定在 profile 并传给 thread/start，原生解析 model 进入 turn.started 和最终消息。续接要求程序、配置、私有 home、权限、模型和来源一致，新 session 使用新 nonce/所属进程范围，不复用旧进程。

isSecret 的原始答案不进入公开事件、审批 response 或 Agent 消息；公开记录只说明该问题已回答。秘密值写入账户私有目录外层的加密脱敏词库，随后原生输出回显、跨增量分片和新 session 续接时继续脱敏。加密文件原子替换，用户行锁串行合并；每账户最多 128 个值，缺少已有词库密钥或容量不足会拒绝继续发送。私有目录与密钥不进入源码包、case 或 S3。这里保证应用公开投影和持久消息的脱敏；CLI 自己的私有历史仍受其原生存储行为约束，不能宣称 CLI 内部历史没有秘密明文。硬 OS 账户/容器隔离继续 J16。

serverRequest/resolved 和匹配原生终态会使待答审批过期。未知 MCP elicitation、动态工具及认证刷新请求尚无产品适配，遇到时失败收尾，不把未知请求当作已批准。

## 完成、取消与 S3

每个原生回合结束后关闭其 CLI session，先核验所属范围真正退出，包括已脱离的后代，再处理 S3。下一条消息新建所属 session，通过显式 resume 续接已核验历史。这个生命周期保证后台命令仍能被退休，也保证同步不会在 CLI 仍可能写文件时进行。前端尚需把新 session/续接隐藏成正常聊天操作。

仅匹配 thread+turn 的 completed 才能进入正常交付。failed、interrupted、超时、断线、输入/协议超限、出生/权限/来源/owner/提交错误均不能发布完成回答。成功收尾重新检查权限及来源，最终 assistant 消息、stream.done 和 conversation owner 释放同事务提交。原生 Goal 的本次完成映射为 waiting_continue，不自动宣称完整工程目标完成。

S3 模式沿用独立 manifest 和 cleanup-only 标记。模型完成、原生后代已退出但同步未结束时，回合保持 finalizing、conversation 保持 active，不发布 stream.done。同步冲突、撤权、来源重绑、取消及永久清理失败保留待处理副本，并令 conversation 需要对账。清理失败后禁止把部分删除的目录再当作新快照上传。已核验、无暂存且终态有效的历史才能随用户明确删除 conversation 一起删除；损坏/未知证据禁止删除。

## 验证与边界

本批原生协议主要通过真实所属 worker 的可控 CLI 替身验证，使用隔离 SQLite 和临时 PostgreSQL；真实 CLI 专项则调用安装的 Codex，通过本地 Responses fixture 验证计划文本、已关闭范围后的显式续接、原生计划问答往返、失败、取消及空账户。没有使用个人凭据或远端模型推理。

具体结果、原始证据路径、约束数量和冻结摘要以 `tmp/mainline-N4-cli-turn-evidence` 的结果及源码匹配文件为准。S3 专项是带内容 ETag 的文件后端替身，不能替代真实远端 S3 验收。旧 Agent shell、CLI metadata、核心及协议辅助套件按此次记录的定向结果验证；未运行的全量 offline/writes/CAE/浏览器、跨主机或恢复快照后重跑不能计为通过。

## 下一单元和完整交付任务

下一单元 g2b3 继续原生账户登录/登出、登录取消与多种认证结果投影、Windows sandbox 状态/需要用户操作的安装流程、WSL 原生 CLI 的发行版/二进制/home/出生边界。随后完成完整聊天前端：后台与前台回合切换、SSE 重连、审批/问答/秘密输入、模型选择、线程显式恢复/分叉、历史分页、账号状态、断线/关闭和对账提示。

还需补齐当前未适配的原生认证刷新、MCP/动态工具/elicitation、附件与图像、usage/预算、完整模式策略、后端切换与历史上下文对齐；全功能计划中的服务端权限、配额、硬隔离和共享组织模型不能因本单元通过而关闭。

原生 CLI 完整接线后复核余下专业入口，继续 N4-04 全入口取消、N4-05 资源预算、N4-06 公平调度、N4-07 活动汇总与统一停机，再推进 N5 owner 恢复/对账/续跑。依赖关系与全部任务仍以 [主线计划](next-stage-mainline-plan.md)、[全功能计划](full-function-delivery-plan.md)、[主线台账](mainline-task-status.json) 和 [295 项台账](full-feature-tasks.json) 为准。
