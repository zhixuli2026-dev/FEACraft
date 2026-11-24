# N4-03.g2a：实际 Codex 协议与所属 stdio 通信

当前 Windows 沙箱的工作台状态、部署初始化命令和真实 DeepSeek 结果见 [FF-F56 Windows 沙箱部署](FF-F56-windows-sandbox.md)。

后续 g2b2 已接持久原生回合/SSE、复杂审批、真实计划问答与显式续接，见 [owned-cli-turns.md](owned-cli-turns.md)。本文保留前序单元的历史范围，早期“尚无回合/审批”不能用来判断当前入口。

后续 g2b1 已接通宿主持久 CLI 会话和正式账户/模型/线程 API，当前边界与下一任务见 [owned-cli-sessions.md](owned-cli-sessions.md)。本文保留 g2a 通信基础的历史范围，不能用早期“尚未接台账”的描述判断当前产品入口。

2026-11-03。本批完成客户端通信基础；产品 API 的用户/会话持久归属继续 g2b。N4-03 不关闭，53 项主线及 295 项功能范围保持不变。

## 已实现的执行链

`CodexStdioClient → OwnedStdio → OwnedProcess → 固定 stdio_bridge → 实际 CLI`。

OwnedProcess 先准备等待放行的根，保留实际创建句柄、出生记录和 Windows Job。`before_release` 回调成功后再次核验根及固定代码，再启动桥接器。桥接器上报自身出生和 profile 摘要；父侧独立打开查询句柄核对出生与 Job 成员关系，等待 `before_start` 回调成功，再允许它启动 CLI。回调收到记录副本，不能替换保留的实际身份。回调失败或取消必须等候范围退出，不能启动 CLI。

这两个回调提供后续数据库登记接点。本批实际测试用检查点证明放行顺序，尚未接入 NativeRequest/NativeSession 或新增 CLI 台账，不能称为已经完成多用户 API 会话归属。CLI 子进程 PID 仅作为诊断；清理由保留的根和 Job 完成，不按该 PID 发送信号。

桥接器使用标准库，在根拥有的范围内创建 CLI。它将 stdout 和 stderr 分别编码成有长度上限的帧；协议客户端只接收 stdout。CLI 输入按 8 KiB 分片，写入实际 stdin 并 flush 后才确认下一片。控制门的停止通道与该数据通道独立，CLI 停止读取 stdin 时仍能终止整个范围和脱离控制台的后代。固定代码发生变化会在放行前拒绝继续。

## 协议修正

本机 CLI 为 `0.164.0-alpha.11.1`。按其 `app-server generate-json-schema --experimental` 输出及 [官方 app-server 文档](https://learn.chatgpt.com/docs/app-server) 修正：

| 操作 | 现在采用的契约 |
|---|---|
| 初始化 | `initialize(clientInfo, capabilities)` 响应后发送 `initialized`，再允许业务请求 |
| 创建线程 | `thread/start`，读取 `result.thread.id` |
| 恢复线程 | `thread/resume(threadId, excludeTurns: boolean)`；不再传入不属于该接口的 lastTurnId |
| 分叉线程 | `thread/fork(threadId, lastTurnId?, excludeTurns: boolean)` |
| 开始回合 | `turn/start`，读取 `result.turn.id` |
| 中断回合 | `turn/interrupt(threadId, turnId)` |
| 回合终态 | `turn/completed` 的 `params.threadId`、`params.turn.id` 和 `params.turn.status` |

只有匹配当前线程、当前回合且 `status=completed` 才返回成功。`failed` 抛出实际错误，`interrupted` 返回中断状态。其他线程或旧回合的完成事件不能结束当前回合。一个高层客户端同时只有一个 turn 事件消费者。Goal 的成功仍返回 `waiting_continue`；现有容量重试只发送短继续消息，不重发原用户消息。

取消、超时或审批回调失败后，等待中断响应及匹配的回合终态，最长 5 秒。只有 RPC 中断响应而没有终态确认时，关闭并核验整个所属范围。取消标志也能打断等待中的 start RPC 和审批回调。未知或部分写入的 RPC 不复用连接，不自动重试副作用。

## 输入、输出与审批边界

单条 RPC 输入/输出最多 1 MiB；桥接帧最多 32 KiB，原生数据片最多 8 KiB；未解析 stdout 缓冲最多 2 MiB。待消费事件最多 256 条/4 MiB，待答复服务端请求最多 128 条，客户端待响应请求最多 64 条。stderr 只保留内存中的末尾 16 KiB，不能作为普通 RPC 事件对外广播。上述是消息和积压限制，整次推理预算及公平队列仍属 N4-05/N4-06。

EOF、无效 JSON、过长行、事件积压、握手错误和写入超时会唤醒等待者并清理实际范围。RPC 期限包含原生 stdin 写入，不仅是等待响应的时间。关闭是同一个可等待操作，不能将取消发送成功当作进程已退出。

审批响应必须对应尚未答复的服务端请求 ID 及方法；并发答复只能领取一次。用户问答必须匹配原问题 ID 集合。通用 `accept` 只用于命令/文件变更审批，权限授予请求采用不同结构，当前明确拒绝套用该响应。没有回调时不自动批准。真实 CLI 的复杂命令/权限/问答闭环仍待 g2b，不以协议 fixture 的通过代替它。

## 账户与测试环境

客户端要求显式提供独立 `CODEX_HOME`，拒绝默认回落到个人 `.codex`。新建空环境的 helper 独占创建目录，写入 `cli_auth_credentials_store="file"`，只继承必要 OS 环境，不继承提供商密钥或代理环境变量。它不导入个人 auth.json，也不读取操作系统 keyring。本批账户读取为 null，本地测试提供商确认没有收到 Authorization 头。

真实 CLI 验证只向本机 Responses fixture 发出三个请求，分别验证完成、明确失败和中断。没有进行账户登录、Windows 沙盒初始化、真实远端模型推理或计费服务调用。沙盒 setup 继续要求用户明确确认。持久私有账户目录、用户撤权、S3 暂存和 WSL 原生 CLI 尚需产品适配；当前 host transport 明确拒绝把 wsl.exe 当作普通宿主 CLI。

## 证据与验收边界

- [7 套定向回归结果](../../tmp/mainline-N4-codex-transport-evidence/protocol/summary.json)：stdio、turn、account、sandbox、capacity、history 和新增所属 stdio 专项。
- [22 项专项结果](../../tmp/mainline-N4-codex-transport-evidence/owned-result.json)：两级启动检查点、登记失败/启动取消、超过 64 KiB 的 Unicode RPC、stderr 分流、并发请求、EOF/非零退出、无效/过长输出、事件数量/字节上限、阻塞 stdin 的超时/取消及真实脱离子进程、重复审批、权限误用、外国/旧回合、等待审批取消和缺少中断终态。独立外部哨兵保持存活，16 次退出回执均验证。
- [真实 CLI 结果](../../tmp/mainline-N4-codex-transport-evidence/actual-result.json)：独立空账户、实际 CLI 的初始化/创建/完成/失败/中断/恢复/分叉和实际范围清理，提供商是本地 fixture。
- [证据来源](../../tmp/mainline-N4-codex-transport-evidence/evidence-origin.json) 保留最终运行路径；较早探索失败不能替代最终结果。

本批不涉及数据库迁移，代码 head 仍为 `d75c32e6b1f8`、25 张业务表；开发库未迁移或重载。没有执行全量 offline、core、writes、CAE 或浏览器验收，也没有声称本批覆盖 PostgreSQL/S3/WSL 产品会话。

## g2b 的执行任务与完成标准

1. **持久准入。** 建立 CLI 会话台账，绑定用户、可选 case、conversation、运行时、服务端选择的私有账户目录、来源与实现摘要。根和桥接出生均在对应启动门放行前提交；租约或权限丢失立即拒绝。加入双库约束、历史保护、复制/回收站/会话删除和恢复只读规则，不通过伪造 ToolCall 复用 Agent shell 台账。
2. **产品后端。** 将服务注册表接到正式 API，接通启动、读取、关闭、账户/模型展示、原生线程和流式回合；以当前会话身份检查每次操作。关闭等待真实退出；重启后历史只读且不重放 start/turn/审批。明确断线与主动取消各自语义。
3. **账户与复杂审批。** 账户登录只能由对应用户操作发起，browser/device completion 按 loginId 对账；秘密不进入事件和公共缓存。按实际版本 schema 接通命令、文件、权限及问答，审批前后复查用户/case/会话/回合，重复或迟到审批不得生效。Windows sandbox setup 继续由用户确认。
4. **case/S3 与 WSL。** 锁内选择来源、锁外启动和运行、停止核验后重验并同步暂存，清理失败保留既有 cleanup-only 意图。为明确配置的发行版选择原生 Linux CLI、私有账户目录和 PID namespace；拒绝 Windows interop 冒充 Linux 归属。补齐真实 WSL CLI 证据。
5. **API 故障验收。** SQLite/临时 PostgreSQL、真实 HTTP 断线/撤权/来源冲突/租约失败、API 猝死、重启只读/无重放及会话关闭；用实际 CLI 与本地 provider 走完整产品路由。S3 可控适配层和远端 S3 分开记录，实际账户/远端推理验收需要用户提供相应环境时再停下来决策。
6. **后续主线。** g2b 验收后继续专业入口复核、N4-04 全入口取消、N4-05 整次预算/配额、N4-06 公平队列、N4-07 统一活动/停机及 N5 恢复。完整功能计划中的原生分支 UI、账户 UX、硬容器、跨主机和远端资源仍按原任务保留。
