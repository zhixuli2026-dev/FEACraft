# 统一聊天中的原生 CLI：N4-03.g2b3d

g2b3i聊天/环境检查已改用持久启动请求；请求丢失可查回、刷新不自动发消息、相同key显式重试、未知结果只读。见[native-cli-startup.md](native-cli-startup.md)。

g2b3h已增加原生模型参数草稿、分页目录候选项与持久参数回执，见[native-cli-model-options.md](native-cli-model-options.md)。

g2b3g已增加原生回合用量折叠展示，明确最近调用/线程累计及未知状态，见[native-cli-usage.md](native-cli-usage.md)。

g2b3f已增加图片选择/粘贴/纯图发送、跨API/原生图片历史与恢复，见[native-cli-images.md](native-cli-images.md)。

后续g2b3e已增加真正的原生线程分叉、刷新发现和不确定结果说明，见[native-cli-forks.md](native-cli-forks.md)。以下为g2b3d历史验证范围。

2026-11-03。本单元在既有 Agent 面板增加 API Agent、Codex Windows、Codex WSL 选择，连接已有的持久原生回合服务。采用已经确认的对外托管方案：网站 Sign in with ChatGPT 身份与 ProviderProfile 模型调用凭据分开，界面不启动个人 CLI 浏览器/设备码登录。

## 已接通的行为

- 原生文本发送、目标回合、显式恢复线程，展示平台对话历史、推理摘要、命令/文件/其他原生条目和运行收尾状态。目标完成后的 `waiting_continue` 仍需用户明确发送下一次请求。
- 创建者可分页查看原生回合，按 `client_message_id` 查找已接受的消息；刷新读取活动归属及持久 SSE，不重新 POST 用户任务。发送响应丢失时只保留消息 ID/会话 ID 并查询，不自动重发或关闭可能已经开始的任务。
- SSE 支持跨字节 UTF-8、CRLF、多行 data、重复事件去重和连续序号检查。断线只重新 GET，保留已接受游标；不完整末包等待重放。浏览器关闭不会取消后台任务；停止按钮调用服务端取消并等待收尾。
- 命令/文件批准、拒绝、会话批准；只应用服务器实际提出的命令/网络规则；权限按请求的权限类别勾选并指定 turn/session 范围；问题选项/自由回答/密码回答。密码输入在提交时清空，不写草稿、URL 或本地存储。服务端仍承担权限上限、一次回应及脱敏的权威验证。
- 独立检查账户凭据绑定、模型列表和 Windows legacy sandbox 准备状态，检查结束关闭临时 CLI scope。Windows readiness 不等于 MXC 或网络隔离证明。WSL 缺少原生持久目录时显示部署配置错误。
- 将原有长工具栏折叠为会话设置，输入框独占一行。在实际 340px 聊天 dock 内没有横向溢出，API 输入宽度从约 77px 增加到 324px；延续既有暗/浅色主题。

后端选择是按用户/会话保存的本浏览器偏好；数据库活动回合优先用于恢复正在运行的后端。该偏好不包含凭据，也不等于服务器共享偏好。运行中可切换查看其他会话；原任务仍由服务端持有。

## API 与上下文边界

| 接口（均以 `/api/v1/codex` 开头） | 契约 |
|---|---|
| `GET /conversations/{cid}/turns` | `limit=1..50`，按时间及 ID 稳定倒序；`before` 必须属于当前用户与会话；返回 `data/next_cursor`，每回合附 runtime |
| 同接口 `?client_message_id=...` | 查找已接受的逻辑消息；不能与 before 混用；没有结果不表示可自动重发 |
| `GET /conversations/{cid}/activity` | 只读投影执行后端/状态和当前创建者的活动回合；未知/损坏记录不伪装 idle |
| `PATCH /conversations/{cid}/settings` | ProviderProfile、模型和权限在算例协调边界内更新；检查会话空闲、未收尾原生执行、待审批工具及 profile 访问权；冲突 409 |

普通回合仍使用已有 `POST /sessions/{id}/turns`、`GET /turns/{id}/events`、审批及取消接口。UI 默认使用后台回合和只读观察；未接入“断线即取消”的前台 `turns:stream` 选项。

普通新线程准入时，在会话执行栅栏内冻结最近最多 32,000 字符的可见文字。选定回答或回合提问进行文字交接时，最多保留 180,000 字符，包括选定位置以前的提问、完整回答及先前选定的交接文本。超长历史带截断标记。文字作为参考附在当前请求之前，新线程的权限与审批由本会话处理。平台落库原始用户消息，并用 `context.handoff` 记录交接长度与摘要值。 [分支与交接验收](codex-branch-handoff.md)

恢复旧线程时，最近可见消息必须仍属于当前用户最后一个已完成的原生回合；后来追加的 API 对话或摘要使恢复请求在准入前返回 `409 input_stale`。用户可以选择新线程接收文字交接。底层运行环境、凭据版本、私有 home、模型等一致性检查继续生效。

## 验证与证据

集中证据目录 `tmp/mainline-N4-native-chat-evidence/`，来源逐项列于 `evidence-origin.json`。所有浏览器操作都在隔离 API/SQLite/本地模拟 CLI 上完成，没有真实模型调用或个人凭据。

| 验证 | 结果 |
|---|---|
| 原生回合正式 API，SQLite / 临时 PostgreSQL | 各 40 项通过，包含新增会话查询、分页、原消息查询、运行中设置冲突和新线程交接 |
| 会话专项 | 5 项通过：响应丢失只查询、跨账户拒绝、API 历史后拒绝恢复、文字交接及无幽灵回合 |
| SSE 客户端 | 8 项通过：UTF-8/CRLF 分包、多行 data、重复/缺口/归属、未知执行、末包及 JSON 错误 |
| 历史文字交接 | 4 组通过，含摘要和中断回答 |
| 凭据 / 双 API owner / S3 暂存兼容 | 3/3 通过 |
| 浏览器 | 文本、刷新审批、停止收尾、密码脱敏、丢失 POST 响应仅一次发送、权限/文件/规则/问题表单、WSL 配置错误及后端切换已操作验证 |
| 前端 | TypeScript/Vite build 通过；静态 UI detector 无发现；独立 UI 审查见证据目录 |

第一次扩展回合测试未通过：旧模拟 CLI 以完整输入字符串作为 fixture 模式，加入文字交接后无法识别 `hold`，导致等待超时。修正 fixture 只从当前消息段选择模式，同时保留完整实际输入日志；随后双库 40/40 通过。没有为通过测试撤去生产交接行为。

复现命令（项目根）：

```powershell
platform/server/.venv/Scripts/python.exe -X utf8 -B platform/server/codex_turn_api_smoke.py --postgres
platform/server/.venv/Scripts/python.exe -X utf8 -B platform/server/codex_conversation_smoke.py
platform/server/.venv/Scripts/python.exe -X utf8 -B platform/server/codex_history_smoke.py
node platform/tools/native-chat-smoke.mjs
```

## 仍需推进

本单元不关闭 N4-03，也不把 295 项功能标为全部完成。继续接入完整 CLI 工作流：真实原生 fork、附件与图像、多模态历史、完整模型/推理参数、MCP/用量、前台断线取消选项及更大规模历史性能。文字交接不等于原生工具上下文复制，平台分叉/交接没有冒充 native fork。

正式公网 Sign in with ChatGPT client/callback、目标机器 Windows sandbox 设置与网络/资源策略、WSL 持久目录和多租户隔离仍需部署验收。当前浏览器证据是桌面工作台及窄 dock，不是整个工作台的移动端验收。本批未重新验证真实 CLI/WSL 模型推理、core/CAE 全集或所有功能。

没有新数据库迁移；head 保持 `1b9a76c0f532`、33 张业务表，没有升级开发库或重载原 8000 服务。源码归档和 SHA 见 `mainline-baseline.md`；回退应恢复至新目录并验证，不能覆盖用户数据或删除运行记录。
