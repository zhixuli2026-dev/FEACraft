# 原生 CLI 分叉：N4-03.g2b3e

后续g2b3f已补充真实WSL原生fork、带图上下文继承与父子历史独立验证，见[native-cli-images.md](native-cli-images.md)。以下为g2b3e当时的验证范围。

2026-11-03。本单元让平台分支对应真正的 CLI `thread/fork`：新线程继承指定回合及其之前的原生历史，随后可独立继续。部署方向沿用对外托管／商业服务的 API / Sign in with ChatGPT；网站身份与模型调用凭据分开。

## 操作与契约

在 Windows/WSL 原生聊天的运行记录中，选择最后一个已完成的本账户回合，点击“原生分叉”。服务先登记新平台会话及持久请求，再创建所属 CLI 会话，调用 `thread/fork`，核验返回的父线程与新线程绑定，登记结果，关闭并核验进程范围，最后才显示完成。新分支默认勾选“恢复原生线程”。普通平台文字分支仍是独立功能。

分叉仅接受最后一个已完成、进程收尾已验证的原生回合。来源会话必须空闲，没有未决工具或原生执行；运行程序、版本哈希、配置、私有 CLI home、ProviderProfile 凭据版本、模型、权限和 WSL 目录/发行版必须匹配。后续平台消息不会进入旧原生回合边界：传入 `lastTurnId` 截断至选定完成回合。任意较早原生回合分叉、跨环境原生历史迁移不在本单元支持范围，可使用已有文字交接。

协议依据为[官方 App Server 文档](https://learn.chatgpt.com/docs/app-server)及安装版本 `0.164.0-alpha.11.1` 生成的 `v2/ThreadForkParams.json`。本实现显式设置 `excludeTurns=true`，并依据本地 schema 设置 `deferGoalContinuation=true`，避免分叉自身触发已有目标的自动推理。真实 CLI 试验验证创建分支时模型请求数量不增加。

## 持久状态和故障

新增 `native_cli_forks`，保存来源会话/原生回合/平台事件边界、目标会话、创建者、请求 ID/哈希、CLI 会话/线程、阶段和结束时间。迁移 head 为 `2c0b87d1a643`，前驱 `1b9a76c0f532`，共 34 张业务表。有分支记录时禁止降级删除该表。本批只升级临时测试库，未升级开发库或重启开发服务。

| 状态 | 含义 |
|---|---|
| admitted | 来源和目标执行权已持久登记 |
| forking | 已登记 CLI 会话，即将或已经发出创建请求 |
| registered | 新线程绑定已持久登记，正在收尾 |
| completed | 新线程绑定、访问权限与进程收尾核验通过 |
| failed | 发出分叉 RPC 前失败，未创建原生分支 |
| recovery_required | RPC 结果或执行归属无法确定，保留证据，禁止重复执行 |
| evidence_invalid（只读投影） | 来源、目标、事件或会话绑定不一致，拒绝作为有效结果使用 |

来源与目标同时持有带租约的会话执行权。跨 worker 读取只报告现状，不能接管或回应另一个 worker 的 actor。服务退出后无法确认的请求保持待恢复，不由 GET 偷偷重跑。来源绑定还会在只读投影与执行准入处核对，防止损坏记录被视作成功结果。

同一来源会话、用户和 `client_request_id` 只有一个请求；不同来源回合复用该 ID 返回冲突。响应丢失时，前端仅保存随机请求 ID 和来源回合 ID，随后 GET 查询并打开已登记的分支，不自动重发 POST。取消会等待所属进程收尾；若分叉 RPC 已发出，收尾成功仍不能证明 CLI 没有创建线程，因此保留 `recovery_required`。持久恢复及管理员对账属于 N5，不能将“已停止进程”写成“分叉未发生”。

## API

均以 `/api/v1/codex` 为前缀，沿用当前登录用户和算例权限。

| 接口 | 行为 |
|---|---|
| `POST /conversations/{cid}/fork` | `{source_turn_id, client_request_id}`；返回持久分支记录，后台执行 |
| `GET /conversations/{cid}/forks` | 当前用户来源分支，最近 50 条；可按 `client_request_id` 精确查询 |
| `GET /forks/{id}` | 查询归属、阶段、收尾结果与 runtime；不输出凭据 |
| `POST /forks/{id}:cancel` | 由当前 actor 所属 worker 取消并收尾 |
| `GET /conversations/{cid}/activity` | 增加 `active_fork` 和 `native_fork`，供刷新后发现任务及分支初始上下文 |

第一条分支消息恢复已登记线程，不重新拼接工具历史。平台历史在原生边界之后已变化时拒绝恢复，用户可显式选择有界文字交接。完成分支可按外键顺序删除；仍有派生分支时不能删除来源；待恢复分支不能被直接删除掩盖。

## 验证和限制

- SQLite / PostgreSQL 分叉 API 各 13 项：边界参数、同请求复用、冲突、第一条消息恢复、父子删除顺序、其他账户拒绝、来源记录篡改、取消、错误父线程、登记失败、RPC 错误、第二 worker 只读、实际 owner 退出。
- 双数据库迁移各 147 项约束检查，34 张业务表、元数据一致、旧历史保持、升级/回滚/再次升级与有记录拒绝降级。
- 原生回合 API 双库各 40 项，协议/会话/历史/owner/staging 兼容 5/5。
- 安装的 Windows CLI + 本地 Responses 服务 5 项：真实独立线程、继承原生上下文、无自动推理、重复请求返回原分支、父子历史独立以及回执不泄漏调用凭据。没有调用托管模型或使用个人登录。
- 浏览器使用实际产品 API 与受控 CLI worker，核验正常创建、继续聊天、父历史不变、响应丢失仅一次 POST、刷新找到原任务、取消后阻止发送、保持后端偏好，以及桌面 340px dock 暗/浅主题。界面复查与逐项证据记录在 `tmp/mainline-N4-native-fork-evidence`。

当前真实 CLI 分叉证据是 Windows；未把既有 WSL transport/turn 验证算作 WSL fork 实测。正式 SIWC client 与公网回调、WSL 持久目录、完整 sandbox 隔离矩阵、附件、多模态、MCP、完整模型参数、用量，以及 N4 其他入口和 N5 恢复继续在原台账推进。53 项主线和 295 项功能范围不变，本单元不代表项目全部完成。
