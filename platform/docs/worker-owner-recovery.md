# 执行主机上的 worker 收尾恢复

2026-11-16，N4-07 阶段实现。整项仍未关闭，主线仍为 48/53。

## 行为

Redis worker 启动后立即运行原有作业恢复逻辑，并按 `job_heartbeat_seconds` 周期继续检查。检查只选择最新 attempt 的原 worker 身份属于本机的记录；完整身份、真实进程退出、原始停止凭据、发布输入和产物仍由既有恢复协议逐项验证。API 的默认启动恢复行为不变。

这补上了远端执行的调用位置：Windows 提交端无法观察另一操作系统主机的进程出生身份，而执行主机上的新 worker 或已运行的其他 worker 可以执行原来的观察与恢复流程。没有增加新的身份替代值、权限层或隔离层。

单个历史记录发生异常不会阻止其他本机记录检查；结果计数保存在 arq context 的 `recovery_outcomes`，扫描次数为 `recovery_passes`。worker 关闭时停止周期扫描，等待当前扫描和它调度的发布恢复完成清理，再关闭 Redis 和数据库连接。

## 真实故障验收

显式入口：`platform/server/live_llm_remote_worker_recovery_acceptance.py`。在原始命令已经退出、原生停止凭据和准备好的发布意图已经持久化、最终 SQL 终态尚未提交时，实际 Linux worker 执行 `os._exit(17)`。它不是模型模拟，也没有伪造 SQL 成功或停止记录。

两种恢复均通过：

- 原 worker 真正退出后，新 worker 启动并恢复原 attempt 的发布提交。
- 已经完成首轮扫描的另一真实 arq worker，在后续周期扫描中发现同伴退出并恢复提交；没有重启该观察 worker。

两次均确认：旧 worker 身份已退出、原生根进程已退出、每个 Job 始终只有一个 attempt，原 worker 身份、进程身份、输入散列和完整 write checkpoint 没有变化；准备时的 bundle 散列也未变化。每个 attempt 只有一个 `attempt.finalization_recovered` 事件，其中 `replayed=false`、`native_execution_started=false`。模型随后通过真实 read_text_file 读回第二个恢复任务产生的随机证明。

最终证据：

- `tmp/remote-worker-h1fpgpn7/owner-recovery.json`：两次故障、恢复前后记录、实际观察者和周期扫描证据。
- 同目录 `remote-result.json`：其余 4 个远端真实任务、取消/关闭/重启和资源清理。
- 同目录 `live-result.json`：DeepSeek `deepseek-flash` 2 次 HTTP 200、真实工具调用及流式回复持久化。
- `tmp/remote-worker-postgres-bf59e15f43bc4800a6d49771a81dec0f/`：隔离 PostgreSQL 数据库已删除，临时 SCRAM 访问规则已移除并恢复原配置。
- `tmp/worker-shutdown-jjwredyh/`：Windows 默认队列 FS/SQLite 下 5 个真实任务、8 个 worker 的关闭与重启兼容回归；另有 2 次真实 DeepSeek HTTP 200。
- 两个成功目录的 `job-exits.json`：独立核验实际原生根进程已退出。

最终两组共 11 个真实任务、4 次真实模型调用、模拟模型 0 次。源审查为 `tmp/N4-07-worker-owner-recovery-review-v2-20261116.json` 和对应 legacy review，共 599 个来源散列。

第一轮 `tmp/remote-worker-x_g9gc9c/` 使用退出码 87；worker 确实退出，但本机 WSL 将该退出码报告为 `WSL_E_TTY_LIMIT`。独立真实命令复现了 `17 → 17`、`87 → 4294967295`。改用 17 后仍核验实际 Linux 出生身份和准确退出码，没有放宽退出证明。首轮不计为通过，没有模型调用，旧 worker、测试数据库、Redis/S3 和临时数据库访问规则已清理。

## 原计划剩余范围

本轮证明的是保留了完整原生停止和发布准备凭据后的进程失联恢复，使用 Windows API 与实际非特权 Linux worker、独立缓存和共享 PostgreSQL/S3。它不证明任意执行中失联都能直接重跑，也不补造缺失的停止凭据。

N4-07 继续处理执行中失联的收尾/接管、缺失或失败的最终排空记录恢复，以及专业 handler 的完整运行环境映射。原 N7 故障矩阵、部署与审计及 295 项全功能要求保持有效；先前未保存响应体的 API 409 诊断缺口也仍保留。
