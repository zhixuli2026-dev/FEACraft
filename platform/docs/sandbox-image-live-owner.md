# 准备取消后的重试与存储预留释放

2026-11-07。本阶段修复两处实际问题：准备任务已失败、子进程已退出时，存活的 API 服务不应继续阻止清理重试；同一次取消留下的 768 KiB 存储预留也应在实际执行停止后释放。主线仍为 33/53 完成、20 项未完成，资源边界 11/17 完成、6 块未完成，295 项全功能目标保持。

## 复现与修复

在真实 `created` 启动观察提交后，取消正在执行的原生任务。生产清理路径关闭实际宿主进程，但完整子进程登记尚未完成，因此原日志没有正常 identity/stop 回执。API 继续运行，旧请求已经失败并记录结束时间；原来的对账仍强制要求 API 所有者退出，导致明确重试持续返回 409。

[修复前复现](../../tmp/sandbox-recovery-ui-retirement-2fgaoc4z/image-live-owner-checks.json)记录宿主真实 `exited`、API 真实 `match`、请求已终止且重复确认仍被拒绝。首次验证脚本误把 Python 启动器 PID 当成 API PID，在[tj61fy1w](../../tmp/sandbox-recovery-ui-retirement-tj61fy1w/image-live-owner-checks.json)失败；改用 API 内实际 PID 与持久出生身份后才得到上述复现。该失败没有算作通过，也没有模型调用。

对账现在区分两种依据：

- `owner_exited`：旧请求仍可能启动工作，必须实际核验所有者退出。历史未携带新字段的回执继续按这一更严格条件验证。
- `terminal_request`：旧请求原本已经 `failed/cancelled` 且有结束时间；`native_request_records._current()` 会拒绝它再次登记或放行。API 所有者可以实际为 `match`，全部已知子进程仍必须实际 `exited`。提交前重新核对完整原行及启动观察，回执保存原阶段和实际观察，不声称 API 已退出。

任何缺失出生身份、活动子进程、身份变化或不完整观察继续拒绝。把实际存活 API 的证明篡改为“所有者已退出”，会使完成查询、重试和来源准入全部拒绝。不会改写原子进程日志，也不会把任务失败伪装成正常停止。

## 存储预留问题

第一版重试修复通过后，检查实际节点预留目录发现每个算例仍残留一份 **786432 bytes** 预留。原来的 finally 仅认可正常 stop 回执，无法利用已知准备阶段出生身份；API 长期运行时，重复取消会累积预留。

现在只在请求已经终止时进行独立只读进程核验。全部已知作用域确实停止、原行保持绑定或已有有效独立对账证明后，释放当前调用持有的那份预留。它不删除原文件，不批量清空预留，也不为出生身份缺失的执行放宽条件。已有文件继续由实际磁盘用量统计覆盖。

## 验收证据

- [SQLite 最终代码](../../tmp/sandbox-recovery-ui-retirement-m6pg9oo0/image-live-owner-checks.json)：真实准备取消、API 保持存活、已知宿主退出、原日志保留、预留释放、明确重试和完成回执；3 项基础设施检查、1 项回执篡改故障检查。
- [PostgreSQL 最终代码](../../tmp/sandbox-recovery-ui-retirement-m_4lnhyr/image-live-owner-checks.json)：同样 3 项基础设施、1 项故障检查，随后真实模型读取恢复文件。独立临时数据库[已删除](../../tmp/image-postgres-igjnsawf/result.json)。
- [活动所有者回归](../../tmp/sandbox-recovery-ui-retirement-1h4hk4mp/image-concurrency-checks.json)：最终代码的双 API、三次取消及实际停止后重试 5 项检查通过；旧请求仍活动时另一 API 继续被拒绝，并未把“服务存活但请求已终止”扩展为活动执行接管。
- 第一版重试修复的 [SQLite](../../tmp/sandbox-recovery-ui-retirement-ta27xojz/image-live-owner-checks.json) / [PostgreSQL](../../tmp/sandbox-recovery-ui-retirement-ni4uwnfa/image-live-owner-checks.json)仅证明重试修复，不证明预留释放；后续实际目录检查才发现这项遗漏。第一份 PostgreSQL 临时库[也已删除](../../tmp/image-postgres-0tfid1_k/result.json)。
- [第一版真实模型](../../tmp/sandbox-recovery-ui-retirement-ni4uwnfa/live-result.json)2 次、8471 tokens；[最终代码真实模型](../../tmp/sandbox-recovery-ui-retirement-m_4lnhyr/live-result.json)2 次、8467 tokens。本轮共 **4 次真实 DeepSeek 调用，16938 tokens，模拟 0**。服务 `api.deepseek.com`，模型 `deepseek-flash`。模型选择真实文件工具，读回从保留快照恢复的随机内容；严格仅回复字符串仍有额外说明，未通过这一格式要求。

两个最终算例各确认 9 个完整登记进程身份退出，准备阶段宿主的退出与存活 API 观察另存于专项记录。前端和数据库 schema 未变更，开发数据库未修改或迁移。

## 继续推进的范围

已知出生身份、请求已终止而 API 仍存活的误阻塞和对应预留泄漏已修复。完全没有持久出生观察、`host_ready` 后尚无 WSL 身份的窗口仍需完整作用域证据；不能据此称所有启动恢复已完成。

PostgreSQL 准备阶段取消已覆盖，但 API 猝死的启动对账、独立事件提交中断、原生 unlink/回执定点故障、权限变化、WSL/主机重启及空间耗尽矩阵继续。原镜像以外的暂存、失败恢复副本、长期快照和额度结算，以及完整恢复语义、RB-12 至 RB-17 和其他全部主线/全功能要求保持原计划。
