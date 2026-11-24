# FF-H02B 作业重启与孤儿恢复验收

原 API 实际退出后，替代进程启动时读取一条损坏的作业输出清单，JSON 解析异常使启动提前退出。后续仍运行的历史记录和已持久化投递的排队作业均留在原状态。旧版的 API 日志和退出前快照已保留。[启动失败日志](../../tmp/live-cli-startup-b1am22t6/api.log) · [退出前快照](../../tmp/live-cli-startup-b1am22t6/job-orphan-restart.json)

启动扫描遇到记录异常后，重新取得算例锁并核对当前尝试的输出清单。只有再次确认清单无法读取或不是 JSON 对象时，才保存现有的人工核查记录并处理下一项。原作业、尝试和损坏内容保留在库中，公开详情显示历史输出问题。原有完整性校验和基础设施错误处理继续生效。

## 两次实际重启

| 数据库与存储 | 进程退出、作业和事件记录 | 真实 DeepSeek 回执 |
|---|---|---|
| SQLite 与文件系统 | [实测](../../tmp/live-cli-startup-6938s8pw/job-orphan-restart.json) | [模型](../../tmp/live-cli-startup-6938s8pw/live-result.json) |
| PostgreSQL 与文件系统 | [实测](../../tmp/live-cli-startup-5zt9unnd/job-orphan-restart.json) | [模型](../../tmp/live-cli-startup-5zt9unnd/live-result.json) |
| SQLite 与 S3 | [实测](../../tmp/live-cli-startup-rsn7yzzw/job-orphan-restart.json) | [模型](../../tmp/live-cli-startup-rsn7yzzw/live-result.json) |
| PostgreSQL 与 S3 | [实测](../../tmp/live-cli-startup-0lkaynkv/job-orphan-restart.json) | [模型](../../tmp/live-cli-startup-0lkaynkv/live-result.json) |

- 一个 Windows 作业在原进程已实际启动、数据库仍记录运行时中断。替代 API 确认原执行实例已退出，将原作业和原尝试标成 `interrupted`，保留空退出码和结束时间，留下唯一的 owner 退出事件。试验进程的启动标记只有一次，第二次重启后事件与状态仍相同。
- 第二项作业的投递和原尝试已持久化，原 API 在 claim 前退出。新 API 在同一 Job 和 attempt 上启动一次，第二次重启没有再次执行。
- 第三项作业在已进入收尾时写入故障清单。两次重启后原内容逐字保留，实际详情与恢复事件保持可读。缺少尝试及投递证明的旧排队记录保留人工核查状态。其他账户访问原作业和损坏历史均返回 404。

四组各进行两次真实 API 重启，执行 12 个 Windows 子进程。资料中的旧归属与损坏输出为明确的测试夹具，其余启动、退出、原生进程观察、数据库和 S3 皆为实测。四组各由真实 `deepseek-flash` 工具回读结果摘要，总计 8 次 HTTP 200，模拟调用为 0。活实例归属和既有 WSL 中断结果见原验证记录。独立 worker 与跨主机协调继续归 H18、J14 和 N7-01。[活实例与 WSL 记录](job-owner-startup.md)

用服务器虚拟环境运行 `live_llm_job_orphan_restart_acceptance.py --source-profile-id 26`，可追加 `--postgres` 与 `--s3`。无数据库迁移，回滚应用版本会恢复旧版对损坏清单的启动行为。[验收入口](../server/live_llm_job_orphan_restart_acceptance.py)
