# 原生 CLI 会话启动与幂等回执

2026-11-03，N4-03.g2b3i。部署约束沿用对外托管/商业服务：网站身份与模型 ProviderProfile 分离，模型调用绑定用户可访问的 API 配置。此增量补齐消息发送之前的 CLI 启动记录，不改变个人 CLI 登录边界。

## 入口与可观察行为

| 入口 | 行为 |
| --- | --- |
| `POST /api/v1/codex/conversations/{cid}/session-requests` | 接收启动请求，先提交记录和会话执行归属，再由后台启动 CLI |
| `GET /api/v1/codex/conversations/{cid}/session-requests?client_request_id=...` | 当前用户的会话启动记录，按请求 ID 查找，最多 50 条 |
| `GET /api/v1/codex/session-requests/{id}` | 读取启动阶段与所关联 CLI 的当前状态 |
| `POST /api/v1/codex/session-requests/{id}:cancel` | 取消当前进程持有的启动执行，并等待收尾；已完成请求返回历史回执 |

请求包含 `client_request_id`、`provider_profile_id`、`provider_wire_api`、`runtime` 和 `purpose`。运行时为 `windows`/`wsl`，用途为 `chat`/`inspect`。同一 conversation、user、client_request_id 的相同参数返回同一记录；改变任一请求参数返回 409。用途记录本次 UI 操作，不增加额外的推理权限。旧的低层 `POST /codex/sessions` 保留兼容，原生聊天和环境检查界面改用新入口。

前端在发送启动请求前保存请求 ID 和选择参数到当前标签页的 sessionStorage，不保存消息、图片或凭据。启动响应丢失后只查询原请求；恢复到 ready 后由用户再次点击发送。尚查不到记录时，可显式“重试同一启动请求”，使用同一 ID 和参数；该操作只启动/找回 CLI，不会发送消息。后台启动不依赖原 HTTP 连接存活，刷新页面不会自动调用 turn/start。刷新会丢弃未提交的消息和图片草稿。

正常的单次发送操作会等到 CLI ready 后继续提交原消息；一旦启动 HTTP 响应失败、组件卸载或用户取消等待，后续查询不会代替用户发送。已准备的 CLI 可以在原配置下使用，改变配置前需关闭它。两个标签页可能找回同一个 CLI；后来的消息被拒绝时保留启动回执，不能自动关闭另一个标签页已使用的 CLI。

## 持久化与启动闸门

迁移 `3d1c98e2b754` 的前序是 `2c0b87d1a643`，新增 `native_cli_session_requests`，共 35 张业务表。记录包括请求摘要、选择快照、可空子会话 FK、阶段、版本和结束时间。请求 ID 同时作为对应 NativeCliSession 的服务器端 ID；子会话记录与父记录 FK 在同一事务登记，随后才能通过原生进程启动闸门。

选择快照冻结 case/conversation/user/runtime、源存储前缀、会话权限、模型、ProviderProfile 描述及凭据 revision。使用现有 case 写协调器与 ConversationExecution 的 owner/lease，启动 actor 提前登记到 supervisor，覆盖 CLI Session 尚未登记的准备阶段。准备、登记和原生 gate 都复核归属、用户访问和配置。启动期间，其他消息执行、配置修改、分叉及会话删除受到同一执行锁约束。

启动总等待上限为 45 秒；底层初始化 RPC 自身也有超时，可能更早失败。取消和停机先收尾启动 actor，再收尾已登记 CLI；准备前失败可以记录 `not_started`，已有进程则必须保留对应关闭证据。

| 阶段 | 含义 |
| --- | --- |
| admitted / starting | 已接收 / 正在准备和初始化；不表示消息已发送 |
| completed | CLI 曾初始化成功并交出使用权；当前是否可用要读取嵌套 session |
| failed / cancelled | 已知失败或取消，且证明未启动或子会话已验证关闭 |
| recovery_required | 归属、租约、关闭或提交结果需要核对，不能自动接管重放 |
| evidence_invalid | 读取发现绑定或证据不满足协议，只读展示 |

`completed` 是历史事实。后续消息完成关闭 CLI 后，启动请求仍为 completed，嵌套 session 为 closed/failed 并带关闭证据；不能把 completed 当作仍可发送的证明。启动未决或证据无效会阻止其他执行和历史删除。删除历史需先验证所有关联原生执行收尾，并先删启动父记录再删 CLI 子记录。迁移降级遇到启动历史直接拒绝，不静默丢弃。

ready 事务确认丢失时，在相同 case 写锁下重新读取提交结果。若已 completed，不再关闭 CLI，因为另一个请求可能已经读取 ready 并提交消息。若无法证明事务结果和当前归属，保留未知状态。接收事务确认丢失而 actor 尚未注册时，也只保留持久记录；GET 和相同 key 的 POST 不会创建第二个 actor。后续对账属于 N5。

## 验证证据

- 后续 WSL 新入口实际矩阵已通过 10 项：全部 6 个 Linux CLI 会话均经持久启动请求创建、相同 key 只返回原会话且初始化不调用推理；覆盖文本、线程恢复、问答审批、真实工具 shell、失败及取消。最终 6 个启动回执均关联已核验的原生退出，测试 Provider 已关闭，私有 Linux 测试目录已移除。使用本地 Responses 服务，无远端推理。证据见 `tmp/mainline-N4-native-startup-wsl-evidence/`；源码随专业入口基线封存。
- SQLite / PostgreSQL 各 16 项：并发相同 key 单次启动、参数冲突、查询无副作用、执行锁、初始化取消/失败/超时、准备前失败、准备阶段停机、凭据 revision 变化、HTTP 响应丢失、ready 提交确认丢失后的回合存活、跨用户访问、未知接收结果以及删除 FK 顺序。
- 完整迁移、约束和降级回归：双库各 164 项检查、35 张业务表、metadata diff 0；只操作临时数据库。
- 兼容 6/6：原生会话、会话发现、凭据、分叉、S3 会话暂存、持久回合 API；其中回合 API 为 SQLite 40 项。此轮没有声称重新跑过 PostgreSQL 全部回合场景。
- 安装的 Windows Codex CLI 3 项：环境检查启动和关闭、显式消息交接、新会话恢复原生线程；使用本地 Responses 测试服务，不调用托管模型。
- 真实浏览器覆盖启动/取消、发送前后响应丢失、同 key 显式重试、刷新无自动发送、输入拒绝后的 CLI 保留、并发发送不误取消、环境检查、键盘关闭与未知结果禁发；实际 340px Agent dock，深浅主题。

规范化证据、浏览器检查清单和独立界面复查见 `tmp/mainline-N4-native-startup-evidence/`。源码封存目标为 `platform/backups/mainline-N4-native-startup-20261103.zip`，实际 SHA 与源码一致性见证据目录。未升级开发数据库、未重启端口 8000、未操作个人 CLI 登录资料。

## 仍在范围内

本轮不关闭 N4-03 整项，也不关闭任何 FF 整项。WSL 新启动入口的上述实际矩阵已补齐，私有目录初始化也已纳入登记后的 Linux bridge，见[所属初始化](owned-cli-state-preparation.md)。进程重启后的未知启动对账、跨标签页完整启动历史入口、私有输入缓存回收、更多模型参数/MCP/动态工具、预算公平与统一停机仍需后续主线工作。GET 不能确认完成时保持阻塞，不宣称后台会自动恢复；元数据检查也不代表 sandbox、网络和租户隔离已被验证。全量 53 项主线、295 项功能范围继续保留。
