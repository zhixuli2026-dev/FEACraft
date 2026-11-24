# FF-H20 通用作业命令过滤与执行回归

2026-11-20，通用类型作业的 S3 输入核验通过 24 MiB 实际输入的提交与发布。使用 1 MiB 读取块，校验过程新增本地副本为 0。SQLite 与 PostgreSQL 均通过内容变更拒绝、旧命名空间、Windows 文件模式和同钥重放检查。[输入校验实测](staging-provision-node-storage.md#2026-11-20-通用类型作业的-s3-输入校验)

2026-11-20，FF-H20（WP01 / R0，T01）完成验收。通用 Jobs 路由先核对算例权限，再匹配现有 `command_safety` 的五类模式，命中时返回 HTTP 400。命令过滤在作业配额、记录写入、投递和启动之前完成。Agent 的 Shell 入口共用同一匹配函数。[通用作业入口](../server/app/routers/jobs.py) · [规则](../server/app/services/command_safety.py) · [Agent 入口](../server/app/services/agent_shell.py)

| 范围 | 实际结果 |
| --- | --- |
| 拒绝与副作用 | 在 `windows`、`wsl`、`wsl-sandbox` 参数下提交五种只会输出文字的测试命令。每个数据库与存储组合均收到 15 次结构化拒绝。作业、尝试、执行头、幂等请求、事件待投递和存储预留的记录计数保持一致，配额检查、投递、启动三个实际调用点的记录均未增加。 |
| 授权与重试 | 其他账户请求原算例返回 404。原文件可读且内容完整。拒绝时使用的幂等键可用于后续正常命令，正常重放返回同一作业。 |
| 正常运行和日志 | 真实 Windows 作业运行成功，增量日志接口和 SSE 均读取到实际输出，SSE 收到终态，进程停止回执通过。 |
| 取消 | UID 65534 的真实 WSL 作业产生启动输出，公开取消接口接纳请求，随后数据库持久化已验证的进程停止回执。 |

SQLite 与 Windows 文件系统、PostgreSQL 与认证 SeaweedFS 各完成一轮。两轮合计 30 次拒绝、2 次成功作业、2 次取消作业。真实模型 0 次，模拟 0 次。已有独立派生回归包含文件系统和认证 SeaweedFS 的派生、超时、失败恢复以及真实非特权 OpenFOAM 执行。[SQLite 结果](../../tmp/job-command-admission-t3s5dcw5/result-job-admission.json) · [PostgreSQL 与 SeaweedFS 结果](../../tmp/job-command-admission-_6hpu5rd/result-job-admission.json) · [派生回归](direct-write-and-derive-capacity.md)

首轮 fixture 在作业显示取消时立即读取持久停止回执，发生空值错误。改为同时等待终态与停止回执。一次并行运行的 PostgreSQL 与 SeaweedFS 复验发生 HTTP 观测超时，保留该轮记录。独立复验测得 WSL 启动请求 8.0 秒，并取得实际停止回执。隔离 API 和对象服务退出，临时 PostgreSQL 已删除。[早读回执](../../tmp/job-command-admission-xj8mag5z/result-job-admission.json) · [并行超时](../../tmp/job-command-admission-hy_oj4g7/result-job-admission.json) · [临时库删除](../../tmp/terminal-agent-postgres-hhaigd56/cleanup.json)

在服务器虚拟环境运行 `job_command_admission_acceptance.py` 可复验文件系统与 SQLite。加 `--postgres --s3` 复验隔离 PostgreSQL 和认证 SeaweedFS。验收通过现有审查登记调用，无部署配置、数据库迁移或生产规则变更。[验收入口](../server/job_command_admission_acceptance.py)
