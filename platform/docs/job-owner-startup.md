# Job 实例身份与启动对账

N5-01续租推进：claim后至原生启动新增监督与核对，staging/旧输入快照转工作线程；封存输入及工作副本批量读取移到锁外，返回后重验完整绑定与发布来源。双库各18项（慢准备、Windows/WSL、五边界取消/心跳失败、输入变更）通过；兼容原8项+收尾定向8项+原生放行16+4项补齐10组。原始失败和测试等待修正均保留；head4e2da9f3c865/35表、53/295及11完成/18实施中/24待实施不变。 详见[启动续租与锁外校验](job-launch-lease.md)。

2026-11-03，N5-01/N5-02 第一单元。修复新 API 启动时误中断其他存活实例作业的问题；尚未交付完整恢复、重新入队、挂接进程或恢复发布。

## 问题与新行为

旧 `main.startup` 查询全部 queued/running Job，逐一调用 `_persist_status(..., interrupted)`。独立双 API 已复现：原作业已进入真实 payload，租约未过期，新 API 启动却将其标为 interrupted；旧 API 仍能响应查询并完成实际清理。原始证据为 `tmp/live-owner-startup-before-7ogb8nhj/before-result.json`。旧夹具的出生观察对象为 Windows Python 启动器；新专项直接读取并独立核验实际 API 进程出生身份。

新 claim 在同一事务保存 `worker_identity_json`，包含 case/Job/attempt/worker、claim 哈希及实际执行实例的 OS 出生身份。提交仍发生在 staging、spawn 和最终放行之前。旧数据默认 `{}`，不能从 worker 名称或 PID 补造出生身份。

启动时按 Job 取得既有 case 控制锁，刷新 Job、活动 attempt/head 和持久 owner，再做本机只读出生观察。锁内没有 WSL 探测、网络调用、进程启动或信号。结果如下：

| 情况 | 本单元行为 |
|---|---|
| 精确出生仍匹配，租约有效或已过期 | 保留原 Job/attempt，由原实例继续执行 |
| 未 claim 的 queued 投递 | 保留，等待后续调度恢复协议；不自动重放 |
| 无 attempt 的旧 Job、旧/损坏 owner、跨 attempt 绑定、外部主机、出生不符 | 保留；无法证明原实例已退出 |
| 精确 owner 已退出，phase=claimed | 原子标 interrupted；记录 owner-exited 审计事件 |
| 精确 owner 已退出，phase=running，原生登记能通过绑定校验 | 原子标 interrupted；不生成实际停止回执，不重放命令 |
| 已退出 owner 的 starting、无效原生登记 | 保留，交给 N5-03 登记窗口对账 |
| 已退出 owner 的 finalizing | 保留发布证据，交给 N5-04；不当作需要重算 |
| 已有终态或并发对账已处理 | 保留终态与历史，不重复写 owner-exited 事件 |

`interrupted` 表示执行实例已退出后的历史状态，不代表残余进程、暂存目录或发布副作用均已核验。原生停止与子执行台账仍按既有证据显示；本单元没有新增恢复操作授权，也没有自动 kill、重领或重试。

## 租约与迁移

默认心跳 10 秒、租约 30 秒；分别可用 `VIBEFLOW_JOB_HEARTBEAT_SECONDS` 和 `VIBEFLOW_JOB_LEASE_SECONDS` 配置。心跳范围 1–600 秒，租约 10–3600 秒，心跳必须严格小于租约的一半。持久时间仍为 UTC。配置只作用于 Job，不改动准备请求、PTY、viz、CLI 或 Agent 的各自租约。

迁移 `4e2da9f3c865` 只给 `job_attempts` 增加非空文本列 `worker_identity_json`，默认 `{}`；业务表仍为 35 张。列内已有任何非空身份历史时拒绝降级，避免丢失归属证据。恢复旧版本需要明确保留这部分历史的方案，不能直接删除记录绕过降级保护。

开发数据库和端口 8000 未迁移、未重载。本单元只在临时 SQLite/PostgreSQL 演练升级、带历史往返、拒绝有损降级、约束和 metadata 差异检查。

## 验证与限制

`job_owner_startup_smoke.py --postgres` 两库各 21 项通过：真实 claim 出生记录与配置后的心跳续租，新 API 不干扰真实运行作业，13 类明确标注的元数据边界夹具，重复对账、双实例争用唯一事件，正常完成，以及 Windows/WSL 实际 owner 猝死后独立核验根退出、保留缺失回执、禁止重放和跨用户读取拒绝。元数据夹具不冒充对应阶段的真实进程故障注入。

最初测试把 venv 启动器 PID 当成 API PID，修正为由 fixture 返回实际 API PID 并由测试进程独立观察；随后增加实际 owner 退出等待，不以启动器退出代替 worker 退出。迁移测试首次 PostgreSQL 在外部 DDL 后复用 `SELECT *` 缓存计划而失败，修正为切换版本后关闭测试连接池。原始失败日志均保留，不计入通过证据。

既有终态专项原先要求新实例将已退出 owner 的 finalizing 改为 interrupted；该断言与保留发布恢复证据的新规则冲突。现改为精确比较原 attempt 不变，仍要求 SSE 只给 `recovery_required`、不能伪造 done；其余八项终态语义继续验证。

规范证据目录：`tmp/mainline-N5-job-owner-evidence/`；具体回归结果与原始路径见其中 `evidence-origin.json`。源码基线为 `platform/backups/mainline-N5-job-owner-20261103.zip`，逐文件核验见同证据目录。文档不据此宣称已运行完整 UI、CAE、真实 Redis 故障或跨主机接管矩阵。

N5-01 尚需覆盖 claim 到 staging/starting 的全生命周期心跳、时钟偏移和失联告警；N5-02 尚需 queued 投递恢复、登记不确定窗口、远端 owner、finalizing/已取消未排空及用户可见人工介入。N4 的权限撤回与会话放行竞态、准备请求跨实例取消、队列、预算、公平与关闭任务继续保留。主线 53 项与全功能 295 项范围不变。
