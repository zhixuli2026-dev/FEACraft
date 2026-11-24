# 原生诊断文件纳入累计输出预算

后续更新：本文末复现的恢复预算绕过现已修复并分批验收，详见[恢复预算](execution-recovery-budget.md)。原始复现与当时未修复状态保留历史语境。

2026-11-06，N4-05 第二批输出预算增量。主线仍为 33/53 完成、剩 20 项，N4-05 保持进行中。

## 两个已验证的问题

前一批管道计数没有包括 Pandoc 辅助进程捕获的 `stderr.bin`。隔离夹具在真实 Pandoc 调用前写入诊断字节，保留实际报告编译。修复前同一作业预算 4096 字节，4 次 worker 调用各输出 2048 字节诊断，仍发布成功；终态只记录 2311 字节管道流量且没有违规。观察为 `tmp/cumulative-output-qtsxhi_o/output-observations.json`，运行日志 `tmp/N4-diagnostic-budget-before.log`，进程 39690 退出 1。诊断字节数来自注入参数和实际成功调用数；旧记录不具备诊断计数字段。

首次接入文件增长检查后，前 8 项 SQLite 检查通过，但“输出 8192 字节后等待 20 秒”的进程没有提前停止。原因是辅助进程的文件写缓冲延迟了可见文件大小，而非测试等待条件错误。失败日志 `tmp/N4-diagnostic-budget-full.log`，进程 19999 退出 1。已将诊断流改为增量读取、无应用层写缓冲，避免少于一块的尾部也必须等到 EOF 才写出。

## 当前行为

原生执行器只对已验证的 Pandoc 配置所在私有目录设置诊断计量器。它在运行中每 50 毫秒观察 `stderr.bin` 增长，并在所属执行停止、管道排空后补充观察。根输出、原生 worker/finalizer 管道和诊断文件增长共享封存的 `max_log_bytes`。超限唤醒原有预算监视器，停止所属进程且不允许发布成功结果。

计量器检查目录与文件身份、普通文件类型及单调增长；拒绝硬链接、替换、截断、消失和特殊文件，不把变化后的文件当作零消耗重新开始。它只检查本次原生调用的已验证路径，不扫描用户整个算例目录。

`attempt.resource_usage` 的新记录使用 `vibeflow.execution-output-usage.v2`，分别记录 `observed_pipe_bytes`、`observed_diagnostic_bytes` 和合计 `observed_output_bytes`，来源表的总和必须等于合计。原有 v1 历史事件不改写。AST/stdout 数据文件不计作诊断，它们仍由待完成的存储预算治理。

这是应用层观察和停止，不是内核硬磁盘限额。两次采样间可能超出预算；单次辅助进程的原有限额继续有效。记录的是实际观察到的消耗，不能声称崩溃后已保存全部字节，亦不能据此关闭持久用量记账要求。

## 验证

- 双库各 9 项：原 5 项管道额度行为，加诊断正常、worker 累计超限、finalizer 累计超限、持续输出后等待；`tmp/cumulative-output-evidence-wpklgvbg/result.json`，进程 81447 退出 0。
- 双库各 4 项 Pandoc 兼容：正常执行、原单次输出上限、worker 取消和 finalizer 取消；`tmp/pandoc-execution-evidence-3eivd1ti/result.json`，进程 94637 退出 0。
- 7 项文件计量完整性检查：增长超限、截断、替换、目录替换、硬链接、目录冒充文件和消失；`tmp/N4-diagnostic-integrity.log`，退出 0。

双库检查均核验所属进程停止、持久终态与发布状态，临时 PostgreSQL 数据库均已清理。新增的部分管道缓冲边界及恢复预算观察以文末最终记录为准。

## 后续范围

恢复代码 `publication_finalizing.reconcile` 和 `publication_preparation_recovery.recover` 当前重建 Claim 后处理发布，没有恢复原执行预算上下文。需要以持久、已排空的原生用量证明恢复预算和原领取截止时间，避免重新给予完整额度或在过期后发布。未知原消耗的旧记录需要明确兼容策略，不能直接当作零使用；正在用真实 API 退出夹具验证该路径。

磁盘/工作副本/不可变版本/S3 缓存、长 I/O、提交后修复、线程池与心跳饥饿仍待。公平队列与关闭策略分别继续归 N4-06、N4-07；全功能 295 项完整保留。

## 最终增量记录

额外的 4097 字节部分缓冲测试双库各 1 项通过：`tmp/cumulative-output-evidence-7y2p4jm6/result.json`，进程 25733 退出 0。原生子进程写入后保持运行，必须在其等待结束前停止；该用例覆盖仅超出 1 字节、没有 EOF 的情况。累计诊断检查为分批 9+1 项/库，不声称一次运行过 10 项。

真实 API 退出的恢复验证确认了上述缺陷：60 秒预算过期后，恢复仍返回 succeeded、发布状态 committed，没有原生重放，也没有 `attempt.resource_usage` 事件。原观察 `tmp/recovery-budget-before-5dakp5ik/recovery-budget-observation.json`，日志 `tmp/N4-recovery-budget-before.log`，进程 37995 退出 1。这是尚未修复的产品问题，不是本批诊断检查失败，也不能作为兼容成功忽略。

下一批应先绑定恢复所需的持久用量和原领取截止时间，验证过期失败、未过期恢复保留用量、未知用量不重置、并发恢复和重复恢复幂等；同时保留已有不可变产物、来源验证、已提交成功及不重放原生程序的约束。所有当前运行均已终止；不要重复修复前复现器。
