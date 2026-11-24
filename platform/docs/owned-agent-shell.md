# N4-03.g1：Agent shell 与工具调用持久归属

最终验证：SQLite/临时PG各32项、可控S3各9项，迁移各70约束及有数据往返/降级历史保护，原请求各14、runtime各25、核心14/14、兼容5/5、启动门8项，以及真实Agent路由custom/Anthropic双协议审批/只读/full_access通过。原始来源及限制见tmp/mainline-N4-agent-shell-evidence。

本增量接通真实 Agent `run_shell_command` 的审批和 `full_access` 两条入口。它复用 NativeRequest，与已有 ToolCall 唯一绑定，不创建工程 Job。Codex stdio/turn、登录与沙盒辅助客户端的实际进程接线仍是后续 g2；完整 N4-03、53 项主线与 295 项功能范围继续保留。

## 授权与唯一执行

审批在算例写边界以条件更新领取 `pending` ToolCall，再提交 running 事件；竞争请求和重复审批返回 409。拒绝也使用相同条件领取，避免覆盖已批准的状态。只有被领取的调用，或当前仍处于 `full_access` 模式的自动工具调用，才能进入执行服务。原始同步 `ToolExecutor.execute` 不再直接启动 shell，缺少调用身份和用户的旁路会被拒绝。

执行服务在新的算例边界重新检查用户状态、case 访问权限、Conversation、ToolCall 的类型/状态/参数和当前权限模式，选择工作目录及固定用途的解释器。随后在同一事务创建 `agent.shell` NativeRequest。它保存会话和工具调用外键，`tool_call_id` 唯一；每个请求最多登记一次 `agent_shell` 子执行，防止同一已批准调用再次产生副作用。

命令等待期间不持有算例写锁。实际 OS 根由 OwnedProcess 创建，准备后保留的进程记录摘要、实际创建句柄、Windows Job 成员关系及 WSL 出生身份必须匹配；身份登记到数据库后，再次核对用户/case/权限/参数/来源、租约与数据库中的子身份，最后释放启动门。实际外部有效进程的出生记录也不能代替本次保留的根。

## 持久模型与迁移

head 为 `d75c32e6b1f8`，仍为 25 张业务表。NativeRequest 增加 nullable `conversation_id`、唯一 nullable `tool_call_id` 及外键，kind 增加 `agent.shell`。CHECK 要求 Agent shell 同时绑定 case、会话及工具；其余请求不能携带 Agent 字段。已有 report/LaTeX/SyncTeX 请求和 case 为空的全局 runtime 请求保持原有结构与语义。

降级必须先明确处理 Agent shell 历史，不能静默删除绑定信息。迁移专项验证前版本带数据往返、外键/唯一性/范围约束、现有 viz/PTY/全局请求历史保护与 metadata 无差异。新迁移只在隔离库运行，开发数据库继续保持旧 13 表及旧 head。

## 执行、取消和输出

支持原有 Windows cmd、PowerShell 和 WSL bash。cmd 固定切换 UTF-8；PowerShell 使用固定系统路径、非交互模式和 UTF-8 输出；WSL 使用配置的明确发行版及被引用的工作目录。请求 profile 保存命令/参数摘要、运行时、来源和时限，数据库事件中的原始工具参数仍遵守既有 API 契约。

时限保留 1–600 秒范围，默认 120 秒。原生输出总量最多 1 MiB，超限停止所属范围；反馈给模型的文本最多 20,000 字符，ToolCall 摘要仍最多 5,000 字符。非零退出作为失败返回，并保留实际退出状态；超时、取消或缺失回执不能伪装成 exit=0。

命令最多 4,000 字符，私有调用使用 UTF-8 JSON 并限制为 64 KiB，避免 Unicode 转义使合法长命令超过旧帧长度。专项包含 3,505 字符的中文命令，使用外部 Python 的显式 UTF-8 输出核对完整参数与回显，同时验证 cmd 的短中文输出。程序自身的编码与长内部 echo 行为仍需继续兼容复核；不把它当成通用原生命令输出保证。确认取消的审批请求会结束流，不再启动模型续跑。

工具文本将原生 NUL 字节显示为 `\0`，避免 PostgreSQL TEXT 拒收时丢失工具回执；专项覆盖真实包含零字节的 PowerShell 输出。

显式消息取消、算例监督取消、真实 HTTP 断线及 SSE 生成器取消，都等待原生任务及其工作线程加入。Windows Job 包含脱离控制台的子进程，WSL 使用已有私有 PID namespace；关闭必须核验实际范围退出。API 猝死后的历史只读，不依靠旧 PID 发信号、不重新执行已批准的调用。未完成的 owner 和停止回执继续留给 N5 对账。

`GET /api/v1/conversations/{cid}/tool-calls/{tid}/execution` 返回当前用户的持久 NativeRequest、子身份/停止证明，以及 ToolCall 的状态和摘要。NativeRequest 状态描述所属执行及收尾，ToolCall 状态描述 Agent 工具回执；调用者可分别检查，不能仅凭内存注册表为空判定成功。

启动中或没有实际退出证明的 Agent 请求会阻止算例复制和移入回收站，即使新 API 已把旧 ToolCall 标记为 interrupted。用户明确删除会话时，只允许连同已核验、没有待处理暂存的原生记录一起删除；活动、缺回执或仍有副本的历史返回 409。

## S3 暂存

正式 S3 模式先复制独立 staging，命令在副本内运行；实际进程退出并登记停止证明后，重新核对 case/权限/参数和来源，在新的写边界同步，再提交和严格清理。同步等待期间 NativeRequest 保持 active，子进程可以已经具有停止证明，ToolCall 仍未成功收尾。

冲突、case 不可用或来源重绑时，不向新来源上传旧副本，失败及副本保留。清理开始前写入已有 cleanup-only 标记；目录锁可限时重试，永久失败保留标记，通用同步拒绝重新上传部分删除的目录。复制期间取消和选择事务提交失败会清理未进入原生执行的副本。取消、执行异常或同步异常时，已进入原生执行的副本保留供检查和恢复，不自动重放。普通非零退出仍同步已经产生的工作区文件，工具回执标错；NativeRequest 的成功仅表示所属执行及收尾完成，不能替代实际退出码和工具结果。

shell 保留已批准用户命令的现有能力。本增量提供实际进程归属，并非每用户硬容器；任意命令对工作区的直接写入、caseDict 镜像修复与不可变产物的完整约束，以及跨主机隔离、cgroup 和网络策略仍须在完整功能计划中继续落实。不得据此关闭相应功能项。

## 验证及后续

`agent_shell_smoke.py` 使用真实 cmd/PowerShell/WSL，覆盖审批前不启动、竞争审批只执行一次、Unicode/非零退出、身份/登记/租约/回执故障、来源与权限复验、取消、超时、输出上限、真实断线、脱离子进程、API 猝死和重启不重放。`agent_shell_staging_smoke.py` 覆盖 9 个暂存场景，使用文件支持的可控 S3 适配层，未使用实际远端 S3 服务。

完整 Agent 兼容测试使用真实路由和本地 mock provider，保留 custom/Anthropic 双协议的审批与只读工具断言，并增加 `full_access` 实际原生执行核验。原请求、全局 runtime、核心和算例管理回归以 `tmp/mainline-N4-agent-shell-evidence` 中最终结果及原始来源为准。

下一单元 g2 接通 Codex stdio 的用户/会话归属、双向 I/O、取消/期限/输出边界及实际 CLI 验证，保留登录、沙盒初始化和命令审批边界。之后继续专业入口复核、全入口取消、整次预算、公平队列、统一活动/停机、N5 恢复和完整界面/跨主机/远端 S3/混合负载验收。本增量没有完成整个 Agent/CLI 功能域。
