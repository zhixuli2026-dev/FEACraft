# N4-03.g2b1：CLI 持久会话与正式 API

当前增量已支持WSL原生会话、Linux私有home和sandbox状态，见[owned-cli-wsl.md](owned-cli-wsl.md)。下文仅为g2b1历史范围；后续原生回合、API凭据和网站身份分别见相关专项文档。

后续 g2b2 已接持久原生回合/SSE、复杂审批、真实计划问答与显式续接，见 [owned-cli-turns.md](owned-cli-turns.md)。本文保留前序单元的历史范围，早期“尚无回合/审批”不能用来判断当前入口。

2026-11-03。本批把 g2a 的所属 stdio 接到正式 API，完成宿主 Codex 会话准入、账户/模型展示、线程创建和清理。完整 g2b 的流式回合、账户登录、复杂审批和 WSL 原生 CLI 继续下一单元；N4-03 及 53/295 完整交付范围不关闭。

## 准入、两级登记与私有账户

新增 `NativeCliSession/native_cli_sessions`，独立于工程 Job、PTY、可视化和 Agent shell 工具请求。记录用户、可选 case/conversation、宿主 runtime、不可变 profile、owner 出生/私有 token 摘要/租约、根/桥接出生、初始化摘要、线程检查点及停止回执。`created_by` 决定历史归属，case 写权限与 conversation/case 绑定在每次有原生动作的操作中重新检查。空 case 的账户会话使用独立私有工作目录，不回落到另一个算例。

先验证用户，再由服务器发现程序并分配账户目录；HTTP 调用不能传入 argv、程序路径、环境变量或账户目录。profile 固定程序/配置摘要、运行目录、case 来源、暂存位置、权限模式和桥接实现摘要。算例准入在协调锁内提交，原生启动和等待在锁外进行。

根启动门准备后，在短事务内比对实际保留记录、出生、owner/profile/lease 并登记，提交后才放行固定桥。桥上报出生后，独立核对保留句柄、Job 成员、profile 和数据库根，再登记并提交，最后释放实际 CLI。初始化响应及 initialized 通知完成后才进入 active。外部有效出生记录不能代替本次保留的根；失去所有权的实例只能停止自己实际持有的范围，不能覆盖新 owner 的台账。

每 5 秒续租并重验用户、来源及会话权限；默认闲置 900 秒后关闭。关闭包含原生范围退出、工作线程加入、S3 收尾和数据库终态提交。请求断线会加入正在执行的工作；初始化阶段关闭和激活后响应阶段断线分别验证，避免关闭任务与已完成启动函数所在的 HTTP task 互相等待。

私有状态默认位于存储目录旁的隐藏 `.private-cli`，也可用 `VIBEFLOW_CODEX_STATE_ROOT` 指定其父目录；解析后的真实路径不得位于公开存储或个人 `.codex` 内。目录绑定 user/runtime，已有目录必须有匹配的 ownership marker。固定 `cli_auth_credentials_store="file"`，不导入个人认证文件。HOME、USERPROFILE、APPDATA、LOCALAPPDATA、TEMP/TMP 指向产品私有目录，只继承必要 OS 环境，不继承提供商密钥。隐藏目录不会进入源码白名单快照，也不进入算例复制或 S3 上传。此处提供逻辑账户隔离，硬容器/OS 用户边界仍按完整功能计划实施。

## 正式 API

| 方法 | 路径（前缀 `/api/v1/codex`） | 行为 |
|---|---|---|
| POST | `/sessions` | 可选 `case_id`、`conversation_id`；后者要求明确 case。只启动服务器选择的宿主 CLI |
| GET | `/sessions` | 当前用户最近 100 条持久历史，不启动进程 |
| GET | `/sessions/{id}` | 当前用户记录、出生和停止证据；不按旧 PID 恢复 |
| DELETE | `/sessions/{id}` | 加入启动/关闭和实际清理；已核验且无待处理暂存的历史可幂等读取 |
| GET | `/sessions/{id}/account` | `account/read(refreshToken=false)`；只投影账户展示字段 |
| GET | `/sessions/{id}/models` | `model/list` 的模型展示字段；不广播未知字段或凭据 |
| POST | `/sessions/{id}/thread` | 先持久领取 none→starting，实际创建后记录 ready/thread ID；重复和竞争请求 409 |

线程创建采用当前 conversation 权限：plan/ask 为 read-only+untrusted，approve_writes 为 workspace-write+on-request，full_access 沿用用户已选择的完整访问模式。全局账户会话使用 ask。这里尚未增加产品 turn/start、线程恢复/分叉、登录或审批 API；g2a 已验证的客户端方法不代表产品入口已经接好。

列表/历史按创建者读取，不暴露 owner token、完整环境、账户目录或诊断尾部。账户/模型只返回允许的展示字段。明确的只读 RPC 拒绝不自动销毁仍可登录或查询的会话；连接、协议、权限/来源或未知执行结果会进入关闭流程。属于其他 API owner 的历史只读，不能借 GET/DELETE/thread 请求再次启动 CLI。

线程 starting 检查点在原生请求前提交；结果丢失后不再次发送 thread/start。尚未调用原生创建的准入中止，使用独立的 `cli-not-started` owner 回执记录，与 OS 停止证明区分。已经出现实际根时必须保存其真实停止回执；缺失或未核验回执不得伪装为 closed。

## case、S3 与历史操作

活动、未知或损坏的 CLI 台账阻止 case 复制、回收站移动及 conversation 删除，包括 API 重启后没有本机注册表的记录。用户明确删除 conversation 时，只能连同已核验、无待处理副本的 CLI 历史一起删除；账户目录不随聊天历史删除。

S3 模式沿用独立 staging manifest。正常关闭在实际范围停止后重验 case/权限/来源，持协调边界同步，再写 cleanup-only 意图并严格清理。同步等待期间父会话仍为 active，不能提前发布数据库终态；原生进程可以已经有停止证明。冲突、撤权/不可用、来源重绑或收尾失败保留副本，不能向新来源上传旧内容。永久清理失败保留 cleanup-only 标记，再次 sync 被拒绝，防止部分删除的目录被当作新快照。未进入原生创建的复制取消或选择事务失败只清理副本，不产生 Job 或原生执行。

关闭接入 case supervisor 的可等待取消及应用 shutdown hook。完整多类资源的统一停机、远端 owner 路由、预算/公平队列和 N5 历史对账仍按后续任务推进。

## 迁移和证据

新增 head `e86d43f7c209`，前驱 `d75c32e6b1f8`，共 26 张业务表。数据库约束包含 FK、唯一 nonce、case/conversation 范围、runtime/phase/version、结束时间及 thread_state/ID 一致性。有 CLI 历史时拒绝降级；没有静默删除 owner、线程或退出记录。开发库不执行本次迁移，也不重载当前开发 API。

- [会话专项](../../tmp/mainline-N4-cli-session-evidence/session-result.json)：SQLite/临时 PostgreSQL，正式 API、实际所属启动门和协议 fixture，包含竞争创建线程、7 类坏台账、外部哨兵、登记/出生/租约/提交/回执故障、来源/权限变化、关闭启动、原生等待不持 case 写锁、真实 HTTP 断线、激活后断线、脱离后代、owner 猝死及重启不重放。
- [S3 可控专项](../../tmp/mainline-N4-cli-session-evidence/staging-result.json)：双库各 9 项；实际原生 fixture 写入文件，采用文件支撑且带内容 etag 的可控 S3 适配层，覆盖同步等待、同大小/同秒变更冲突、来源重绑、case 不可用、清理重试/永久失败、复制取消和选择提交失败。不是远端 S3 服务验收，也不是实际 Codex 推理写文件验收。
- [实际 CLI API](../../tmp/mainline-N4-cli-session-evidence/actual-result.json)：真实本机 Codex `0.164.0-alpha.11.1` 经正式路由完成空账户、模型列表、线程创建、重复保护、关闭与历史删除；没有发起模型推理或登录。
- [迁移专项](../../tmp/mainline-N4-cli-session-evidence/migration-result.json)：双库约束、带数据前版本往返、历史降级保护及 metadata 无差异。
- [兼容与核心](../../tmp/mainline-N4-cli-session-evidence/evidence-origin.json)：复验原 Agent shell 双库各 32 项及隔离 core 回归。最终来源和运行顺序以此文件为准；探索失败保留原始记录，不替代最终结果。

本批未做全量 offline/writes/CAE、浏览器、硬隔离、跨主机或真实远端 S3 验收，不能据此关闭完整 CLI/Agent 功能域。

## 下一单元 g2b2 的必做任务

1. **正式原生回合。** 增加持久 turn/客户端消息幂等、conversation 原生 thread/turn 绑定和统一执行互斥；与现有 API Agent/Goal 的活动状态兼容。请求期限、取消和断线加入实际回合及清理；仅匹配的原生 terminal status 可形成回执。
2. **流和持久事件。** 接通 SSE/重连、消息增量、工具生命周期、最终回答和错误，事件先持久后发布，秘密与诊断分流。消息/事件积压与整次模型预算分别处理；连接断开与明确停止的产品语义需要在实现和 UI 中一致。
3. **复杂审批。** 按本机 schema 接通命令、文件变更、权限和用户问答，回合/请求 ID 双重对账；权限授予不能套通用 accept。审批领取、防重复/过期、用户撤权与权限模式变化均重验，不跨重启重放副作用。
4. **账户与 sandbox。** browser/device 登录由对应用户明确发起，loginId 匹配完成和取消；令牌只留私有 CLI 存储，不进事件/缓存/备份。Windows sandbox setup 保留用户明确确认。用可控 provider 先完成实际 CLI 的产品回合闭环，需真实账户或费用决策时再请求用户输入。
5. **后续能力。** 正式 resume/fork 和原生历史对账、创建请求幂等、完整分页、WSL 原生可执行程序/私有目录/namespace、case/S3 的真实推理写入、前端入口和账户/审批 UX。随后按原计划继续专业入口复核、N4-04/05/06/07 和 N5，不减少 53/295 范围。
