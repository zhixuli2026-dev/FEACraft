# N4-04：持久终态、兼容产物收尾与跨 API 日志

最终结果：终态SQLite/临时PostgreSQL各9项；Pandoc扩展各19项、LaTeX各15项、发布竞争各10项通过；core14/14、execution3/3、父作业收尾通过。749文件源码基线与完整结果来源见tmp/mainline-N4-terminal-evidence。最后新增的旧记录nullable时间兼容由两库终态9项覆盖，其余现代原生路径行为不变。

日期：2026-11-02。本批修复 Job 的逻辑退出码、结束时间和 SSE 收尾条件；主线 N4-03/04 继续实施。完整范围仍为 [53 项主线](next-stage-mainline-plan.md) 与 [295 项功能](full-function-delivery-plan.md)。

## 逻辑结果与原始进程结果

此前 worker 可以正常 exit 0，但独立产物校验、依赖或清理回执校验随后失败。数据库正确保存 failed/-1，本机 `JobProcess` 却保留原始 exit 0，因此本机和另一 API、重启后的读取结果不同。

`job_execution.finish(return_result=True)` 现在在终态事务提交后立即捕获不可变 `Completion`：status、逻辑 exit_code、数据库 ended_at 和 cleanup_verified。执行器在结束通知前采用这个快照，不再从原始根进程退出码推断最终结果，也不为同步结果额外读取数据库。原始 OS 退出码继续保存在 `attempt.process_stop.root_exit_code`。

普通服务调用不传 `return_result` 时仍返回原有状态字符串。快照在可选别名修复、失败回退之前捕获，避免后续 rollback 导致 ORM 属性过期。可选失败回退报错的日志也使用已保存的 owner ID，避免已提交的失败结果因一次额外 ORM 读取而再次变成收尾错误。

旧记录可能既没有 attempt，也没有结束时间。其原有终态与退出码继续可读，ended_at 和 cleanup_verified 保留 null，不用当前时间或猜测的清理结果补齐。已登记原生身份的新执行仍要求真实终态时间和匹配的持久记录。

GET 作业、列表、日志轮询及 SSE 使用已提交的逻辑退出码和结束时间。已持久接受的取消立即可从两个 API 读取；这仍是取消请求状态，实际收尾由 attempt、停止记录和 SSE 完成事件区分。既有 Job 的取消 ended_at 仍为请求提交时间；真正原生停止时间见停止记录，不能把该 Job 时间字段解释成清理核验时刻。

## 成功发布与兼容产物收尾

不可变产物版本仍在主事务中提交，兼容文件随后通过已有的别名修复协议写回。最初的终态展示修改曾过早暴露 succeeded，兼容回归在随后读取 HTML 时得到 404。此失败保留在本批证据中，未把它当成通过。

成功发布时，同一主事务在 `attempt.output_manifest_json.completion` 保存 `vibeflow.job-completion.v1` 的 pending 标记。别名写回完成或明确失败后，原 owner 再核对 claim/head，持久保存 settled、alias_error 和 UTC settled_at，并写入 `attempt.completion_visible` 事件。此字段采用已有 JSON 列，本批没有新增表或迁移。

各 API 在 pending 期间返回 `completion_pending=true`、running/finalizing 展示状态；列表、幂等重放和日志轮询采用相同判断。SSE 同样等待这个持久收尾标记。已提交的不可变版本不会被迟到取消撤销；该阶段的取消响应明确说明结果已经提交、仍在完成产物收尾。

别名修复失败仍保留真实的成功版本，并在 settled 中记录错误类别；用户可通过不可变产物接口读取正确结果。写入收尾标记失败则保留 pending，本机 SSE 报 persistence_failed，远端在所有者租约过期后报 recovery_required，不能伪造完成或自动重跑原作业。pending 阶段会继续更新所有者租约。标记缺失的旧记录保留兼容读取，损坏标记不会被当成已完成。

## 另一 API 上的 SSE

此前 API 没有本机进程对象时，日志流读取一次文件便发送 done，即使作业仍在另一进程运行。现在它使用短时独立只读会话轮询持久状态，并在请求授权后释放原数据库连接。16 个并发远端 SSE 订阅的测试验证后续 API 查询和终态写入仍能进行。

远端流分块读取持久日志，等待 attempt 终态、匹配的根停止回执以及成功发布的 settled 标记。已取消但仍在收尾时不会提前 done。已有停止回执明确为未核验时，完成事件保留 cleanup_verified=false；缺失、损坏或所有者消失时分别返回可恢复错误，不能把“本机没有进程”当成完成证据。

完成事件包含 status、exit_code、ended_at、cleanup_verified，本机和远端采用同一结果语义。cleanup_verified 汇总根停止记录和全部子执行停止回执；没有原生执行的排队取消或旧记录为 null。owner 猝死且没有已提交停止回执时不发送 done，即使旧启动对账随后将 Job 标成 interrupted，也会提示恢复对账。

本机输出和持久日志流都使用增量 UTF-8 解码，测试让中文字节跨越 4 KiB 和 64 KiB 边界。关闭查看日志的连接只取消订阅，不取消工程作业。流读取不接管作业、不发送进程信号，也不改写工作流状态。

## 验证

`job_terminal_smoke.py --postgres` 使用两个隔离 API、命名临时 PostgreSQL/SQLite 和真实 Windows worker，覆盖正常结束、exit 0 后的逻辑失败、可选回退失败、未核验清理、远端取消、查看连接断开、排队取消和实际 owner 猝死。它比较作业详情、列表、日志轮询、本机/远端 SSE 以及重启读取；保留原始退出证据，并检查恢复错误不会伪装成 done。

Pandoc 专项补充真实报告的别名写回屏障、别名失败保留不可变版本、第二次收尾回执提交失败，以及两阶段子执行失败后的逻辑退出码。屏障期间两 API 均不能提前宣布成功；释放后实际 HTML/DOCX 兼容产物可读。LaTeX 原生专项同时核对逻辑退出码与新 API 结束时间。

最终通过结果、原始来源和先后顺序汇总于 `tmp/mainline-N4-terminal-evidence/`。当前运行的完整组合以该目录实际结果为准；未通过的早期运行保留作定位证据。

## 后续任务

N4-04 仍需完成排队重投、claim/启动门前后、跨 worker 通知失败与重复取消的完整竞争矩阵，并与 N4-05/06 的预算、配额释放和公平调度组合验收。成功版本已提交但收尾标记缺失时的租约恢复、条件化别名修复和补偿继续由 N5 完成，不自动重放求解、编译或用户命令。

N4-03 继续 [锁外准备请求归属、SyncTeX 与其余会话入口](owned-pandoc-children.md)。多版本 API/worker 混跑、远端资源和完整 UI 恢复体验仍按原计划逐项验收，本批不关闭整个 H01 或任何全部功能范围。

实现：[job_execution.py](../server/app/services/job_execution.py)、[jobrunner.py](../server/app/services/jobrunner.py)、[jobs.py](../server/app/routers/jobs.py)、[终态专项](../server/job_terminal_smoke.py)。
