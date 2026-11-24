# Job 完整收尾的第一轮实现

日期：2026-11-02。本文件记录首轮收尾修正及其延续；随后N1-01/02已验收，新增表/状态机/Job事务outbox见 [workflow-persistence.md](workflow-persistence.md)。N1-05仍在实施，状态见 [mainline-task-status.json](mainline-task-status.json)。尚无新增工作流API，不能把局部修正称为完成N1。

## 当前行为

子进程退出后提交JobAttempt.phase=finalizing，并同步内存phase，按 finish hook、专业求解证据、必要 staging 同步、日志摘要、Job 终态提交的顺序收尾。`JobProcess.wait()` 等待这条完整路径；Redis worker 和本地批次均改用 wait，不再把 on_finish 回调当完成通知。可选索引/成功 staging 清理失败记录警告，不伪造专业失败；同步失败仍为失败且保留 staging。

`_persist_status` 的数据库错误向等待方传播，终态提交使用旧状态条件更新。成功、失败、取消、中断终态不被迟到回调重写；已经提交的取消在完成回调竞争中保持取消。终态时间保留，重复相同终态只补旧数据缺少的 ended_at。普通列表/详情和增量日志不会在 finalizing 显示 succeeded；日志 SSE 只在提交完成后 done，持久化失败则返回 error/persistence_failed。订阅判定使用 finished，不再仅靠较早设置的 ended_at。

API兼容字段保留，详情/列表额外有`phase`、`completion_error`。新详情包含持久attempt摘要，历史详情可查phase；completion_error仍为内存，列表phase聚合未补齐。这不代表跨重启恢复已完成。随后Job状态与outbox已同事务提交，投递失败退避重试；消费者/索引完整幂等仍待N1-06/N6验收。

## 实际验证

`server/job_finalization_smoke.py` 使用真实 Python 子进程、独立 SQLite 和文件屏障，执行8项硬断言：

1. finish 发布和真实 DB commit 前，worker 未返回、公开状态为 running，晚订阅没有提前 done；提交后才释放等待方。
2. 退出码0但专业证据无效时失败。
3. 上传失败时失败并保留 staging，不调用成功清理。
4. DB commit 异常传播；DB 保持 running/无 ended_at；SSE 明确 error、无 done。
5. succeeded/cancelled 并发终态更新只有一个获胜，迟到 failed 不改终态。
6. DB 取消先获胜后，完成回调保持 cancelled 并正常结束订阅。
7. 已提交结果不因可选事件通知异常而变失败。
8. 所有观察到的 jobs.updated 通知均在对应 DB 状态之后。

这里直接执行真实 `distributed_queue.run_job` 函数与 JobProcess，**没有启动 Redis/arq 运输层**；真实Redis运输层仍待验证；双独立worker原子claim、HTTP幂等和跨API重启随后已通过，见 [job-execution.md](job-execution.md)，不能以此测试替代分布式验收。

首轮命令为`python tools/run_regression.py --profile offline`（当时13套），及`--profile execution`（3套）。最新offline已扩为19套，core14与真实CAE3保持；原日志在`tmp/mainline-N1-finalization-evidence/`，后续在`tmp/mainline-N1-durable-evidence/`。目前Python入口177个，没有重跑全集。

## 剩余边界与回退

phase已接入实际JobAttempt，完成错误和等待对象仍在内存，重启仍使用旧孤儿政策。spawn前原子claim已阻止双worker重复启动；过期租约不自动重领。取消仍主要杀父进程，跨线程报告发布、发布后取消、进程树与资源竞争未解决。活动发布代次尚未实际接入，文件与DB间的崩溃仍待提交日志恢复；outbox自身的补投已有证明，但不能代替产物恢复。

N1-03依赖/确认核心已接入；Job收尾与step/run/事件同事务，见 [workflow-scheduler.md](workflow-scheduler.md)。下一步N2写入协调，同时补N1-04专用入口和N1-05/06剩余边界。尚未完成53项或295项功能，不关闭整项FF。回退使用版本归档在新目录恢复，保留新运行数据；新增迁移只在临时SQLite/PostgreSQL执行，既有开发库未升级，原开发服务未重载。
