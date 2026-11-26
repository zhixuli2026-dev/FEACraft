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

## Linux Agent 命令内核额度

Linux 的 `run_shell_command` 使用已批准的同一 POSIX 命令、算例与会话绑定。启用 CPU 或内存限额后，API 在建立 `NativeRequest` 前核验控制器。每个请求固定保存策略。受信执行门把准备中的 PID 命名空间加入专用 cgroup，在已登记进程身份并再次核对审批及算例权限后才放行命令。内核累计该命令及其后代的 CPU，内存按同一作用域核验。 [部署参数](deployment.md) · [Agent 执行](../server/app/services/agent_shell.py) · [原生请求接入](../server/app/services/native_request_execution.py)

启用但缺少控制器权限时，工具返回 `agent_compute_unverified`。触发内核限额时返回 `agent_resource_limit`，执行详情显示 `cpu_limit` 或 `memory_limit`、退出证明、冻结策略及实测用量。Linux 命令只接收工具路径、所需区域设置、算例 `HOME` 和临时目录设置。 [任务台账](full-feature-tasks.json)

2026-11-26，在实际 Linux Docker cgroup v1、PostgreSQL 和文件系统上，由授权 `deepseek-flash` 选择 `run_shell_command`，再从正式接口批准。两个 CPU 子进程的累计用量达到 1,014,611,500 ns，触发 1 秒限额并取得 `cpu_limit` 停止回执。160 MiB 分配在 64 MiB 内存与交换上限下触发一次内核 OOM，记录峰值 67,108,864 字节及 `memory_limit`。同一 API 的另一账户完成了实际文件读取，记录 69,310,600 ns CPU。全部四条已启动请求均核验进程范围为空，控制组回到零。

复验还覆盖默认关闭限额、Linux 命令环境、缺少控制器权限、跨账户执行详情及对端 API 读回。启用与关闭限额的正常命令均确认测试标记和 API 连接、签名及 vault 路径变量未出现在子进程环境。缺权限入口在登记前拒绝，另一账户查阅执行详情返回 404，对端 API 返回一致的停止回执。最终轮共 10 次真实 DeepSeek HTTP 200、10 个不同的完成标识，模拟调用 0 次。 [最终回执](../../tmp/j16-agent-linux-20inena2/agent-compute.json) · [真实模型验收入口](../server/live_llm_agent_shell_linux_compute_acceptance.py)

早期试跑记录了共享卷测试账号无法写入、提示中的运行时字段不符合 Linux 工具定义，以及 CPU 测试脚本转义错误。最终验收使用同一服务 UID 访问隔离共享卷，按 Linux 工具定义发起真实选择，并在提交前编译测试脚本。隔离容器、卷及网络已清理，原 11 个服务标识一致。C 盘可用空间由 39,740,526,592 B 变为 39,748,575,232 B，Docker 构建缓存维持 5.857 GB，本阶段回收 0 B。 [磁盘读数](../../tmp/ff-j16-agent-compute-20261126/disk-after.json) · [Docker 占用](../../tmp/ff-j16-agent-compute-20261126/docker-after.txt)

## Linux Agent 命令网络隔离

原生 Linux `run_shell_command` 默认在专用 PID 和网络命名空间执行，独立于 CPU 或内存额度设置。受信启动门读取本机网络命名空间身份和待执行命令的网络身份，确认不同后写入原生执行回执。放行前再次核对内核身份和已登记的原生进程。环境无法创建或核对私有命名空间时，命令在真实业务放行前退出。现有历史回执继续可读。 [启动门](../server/app/services/process_gate.py) · [进程归属](../server/app/services/owned_process.py) · [请求回执验证](../server/app/services/native_request_records.py)

2026-11-26，隔离 Docker 复验在同一 API 网络中启动两个实际 TCP 监听器，分别使用容器 IPv4 地址和 IPv6 回环地址。API 网络中的对照连接读到监听器返回的标记。正式 Agent 工具经真实 DeepSeek 选取并批准后，计算额度开启与关闭的两种配置均只看到自己的 `lo` 接口，IPv4 连接返回 `ENETUNREACH`，IPv6 连接返回 `EADDRNOTAVAIL`。工具启动的后代进程与工具本身使用相同网络命名空间，身份与持久回执吻合。IPv6 回环仅在测试容器启用，以建立可达的对照。 [最终网络复验](../../tmp/j16-agent-linux-rf2rjmbr/agent-compute.json) · [真实模型验收入口](../server/live_llm_agent_shell_linux_compute_acceptance.py)

独立的 UID 65534 启动门同时通过实际运行、命名空间核对及退出证明。CPU、OOM、缺控制器权限、跨账户 404 和跨副本停止回执通过复验。最终真实 DeepSeek HTTP 200 和不同的完成标识各 10 次，模拟调用 0 次。Linux Agent 命令的 IP 出口已验收。

隔离容器、卷及网络均已回收，原 11 个 Docker 服务标识一致。C 盘可用由 39,747,219,456 B 变为 39,745,802,240 B，构建缓存 5.857 GB，回收 0 B。 [磁盘读数](../../tmp/ff-j16-agent-network-20261126/disk-after.json) · [Docker 占用](../../tmp/ff-j16-agent-network-20261126/docker-after.txt)

## Linux Agent 命令文件系统隔离

原生 Linux Agent 命令在独立挂载和 PID 命名空间中创建临时根文件系统。受信启动器按原绝对路径挂载当前算例或 S3 暂存目录，供命令正常读写；系统和随镜像安装的工具目录通过只读绑定提供。`/tmp` 属于本次命令，`/proc` 是本次 PID 命名空间的只读视图。切换根目录并卸载原根后，启动器清空命令能力集，再启动已批准的 POSIX 命令。当前请求保存并核对启动器 SHA-256。 [Linux 启动器](../server/app/services/agent_shell_linux.py) · [请求绑定](../server/app/services/agent_shell.py) · [Linux 根挂载切换](https://man7.org/linux/man-pages/man2/pivot_root.2.html) · [执行权限约束](https://github.com/util-linux/util-linux/blob/master/sys-utils/setpriv.1.adoc)

2026-11-26，本地文件系统的隔离 PostgreSQL API 复验通过。真实 `deepseek-flash` 选择并经接口批准的命令在额度开启和关闭两种配置下均读写当前算例，从 API 逐字节读回输出。另一账户的实际对照文件无法读取，工具目录拒绝写入，临时文件未出现在容器宿主视图。实测 `NoNewPrivs=1`，命令五组 Linux 能力均为空。独立 UID 65534 实测保留输出文件属主和完整停止回执。IPv4、IPv6、CPU、OOM 和跨账户回执复验通过。10 次真实 DeepSeek HTTP 200，模型模拟调用 0 次。 [本地文件系统回执](../../tmp/j16-agent-linux-9aercnmc/agent-compute.json)

认证 SeaweedFS 复验中，Agent 从已登记的 S3 快照暂存目录读取输入并写出标识文件，原 API 完成同步及清理。另一 API 使用独立的空文件缓存，直接从同一认证存储读回相同字节及同一条已核验退出回执。命令无法读取挂载范围外的实际本地对照文件，成功结束后原 API 剩余暂存目录数为 0。S3 轮有 2 次真实 DeepSeek HTTP 200，两种后端最终轮合计 12 次，模型模拟调用 0 次。 [认证 S3 回执](../../tmp/j16-agent-linux-06mdpgy7/agent-compute.json) · [真实验收入口](../server/live_llm_agent_shell_linux_compute_acceptance.py)

隔离容器、卷及网络均已清理，原 11 个 Docker 服务标识一致。C 盘可用由 39,744,159,744 B 变为 39,743,496,192 B，构建缓存 5.857 GB，回收 0 B。FF-J16 仍覆盖用户常驻环境及其他执行入口的统一隔离。 [磁盘读数](../../tmp/ff-j16-agent-filesystem-20261126/disk-after.json) · [Docker 占用](../../tmp/ff-j16-agent-filesystem-20261126/docker-after.txt)

## Linux 账户 HOME

启用账户状态卷时，Linux Agent 命令使用同一账户的普通 Linux 终端 HOME。算例工作目录、S3 暂存写回、命令授权和默认网络隔离继续由原执行入口处理。父进程按工具调用者选择 HOME，工具参数无法选择其他账户目录。 [终端行为和验收](owned-pty-sessions.md#linux-账户-home-持久化)

使用采样容量控制时，账户 HOME 超限会返回 `agent_resource_limit`。运行中触发会停止已登记的子进程范围，并在执行详情记录验证过的 `disk_limit`。放行前发现超限会拒绝执行。运行失败的算例暂存副本保留用于恢复。 [账户 HOME 容量控制](owned-pty-sessions.md#账户-home-容量控制)

启用 ext4 硬配额后，Agent Shell 与普通终端在同一账户目录里分配的块共享硬上限。命令收到内核的 `EDQUOT`。未配置或无法核验项目的账户在放行前收到 `agent_resource_limit`。 [内核硬配额及验收](owned-pty-sessions.md#账户-home-内核硬配额)
