# 依赖调度、失败传播与工程确认（N1-03）

报告增量：report.build消费同run已确认postprocess publication；E确认提交后准备失败保留pending，dispatch重试不能替换生产版本。报告可成功保存为superseded历史，当前资格由持久head决定；见 [report-workflows.md](report-workflows.md)。

后处理增量：postprocess消费同run已确认solve及其mesh版本，E确认绑定本run后处理版本。D确认提交后准备失败保持pending；重试与确认刷新均不能换另一运行生产者。真实比较失败保留诊断但阻止E/报告，见 [postprocess-workflows.md](postprocess-workflows.md)。

串行求解增量：solve步骤消费同run已确认mesh publication，D确认绑定本run的solve版本；后续后处理接线见本文顶部。失败、源变化、旧运行迟到或取消不能借新版本完成确认，见 [solve-workflows.md](solve-workflows.md)。

后续网格增量：同run的mesh步骤绑定已确认geometry生产版本；B确认提交后派发mesh，失败保留ready并返回原因；C确认绑定mesh生产版本及上游依赖。详见 [mesh-workflows.md](mesh-workflows.md)。

日期：2026-11-02。N1-03的调度核心按本页证据验收。N1-04/05/06及N2–N7仍有工作，全部目标仍为53项主线和295项功能；本页不表示用户已能从页面发起完整自动CAE工作流。

2026-11-02后续增量：v2注册计划、几何preview/export与6个工作流API已接通；B确认绑定本run生产publication，不能借其他流程结果。当前边界及双库各15项证据见 [registered-workflows.md](registered-workflows.md)。下面N1调度核心记录保留当时验收范围。

## 已实现的调度核心

`services/workflow_scheduler.py` 生成固定、有版本的服务端计划：preflight → geometry → confirm_B → mesh → confirm_C → solve → confirm_D → postprocess → confirm_E → report。目标决定终点；已处于B/C/D/E的算例从后续步骤开始，preflight会复核已有阶段证据。V必须先通过原阶段接口显式回A。客户端不能传任意DAG、handler导入路径或跳过确认节点。

`create_run` 先校验case写权限，组织沿现有授权/配额上下文解析；case+发起用户+幂等键保持唯一。相同规范化请求返回原run，冲突返回idempotency_conflict；run、全部steps和workflow.created/outbox同事务。记录caseDict版本、原文哈希、工程指纹和预算，工程输入不是只看DB镜像。当前记录并非冻结输入目录，后者属于N2。

`reconcile` 在case控制锁内读取持久记录：只有依赖全部succeeded才将步骤置ready；上游失败、中断或取消会阻断后续步骤。`queue_step` 重新检查依赖/版本/输入指纹/预算，并在同一事务创建关联Job/attempt，绑定active_attempt_id。两个调度者竞争同一步骤只能有一个排队成功；回滚不会留下孤立Job或占用步骤。

真实JobProcess仍由统一执行器领取。Job进入finalizing或完成时，`job_execution` 在相同事务调用工作流对账，更新step/run及事件；不是依赖浏览器轮询才能完成。全部步骤成功才完成run，取消和中断保留对应终态。所有关联attempt/Job事件带run_id/step_id。旧原子claim还检查run是否取消以及step活动attempt指针，防止被替换的步骤回调继续执行。

N1完成的是依赖与状态调度核心；后续N3已补注册几何适配和显式派发，N6已补首批创建/列表/详情/取消API。网格及其他专业适配、自动后续派发循环、恢复接口及页面继续待实施。暂未开放一个看似可用但没有专业适配器的工作流启动按钮。

## 显式确认与原接口兼容

原 `POST /api/v1/cases/{id}/caseDict:advance` 保留state、confirmed、expected_revision语义，可额外传：

```json
{"state":"B","confirmed":true,"expected_revision":4,
 "workflow_step_id":"<waiting-step-id>","expected_step_version":2}
```

传workflow_step_id时必须同时传expected_step_version。先校验case写权限，再校验step属于该case、run未终止、节点确实等待当前目标阶段、步骤版本和等待时的caseDict revision/原文哈希。无权case/外部step按404处理；过期步骤、revision或未明确确认返回409，缺配对字段返回422。

`engineering_stage.py` 从原接口提取同一套顺序、工程证据、未结清错误及镜像更新逻辑。工作流确认复用它，不另设放宽的阶段旁路。通用状态机仍禁止awaiting_confirmation直接改succeeded。确认后原case阶段变更与step/run控制记录由同一调用者提交，只有紧接的依赖节点被释放。旧调用未传工作流字段时行为保持。

等待期间编辑caseDict会使旧确认失效。`refresh_confirmation` 必须带当前step版本，且工程指纹仍匹配，刷新后获得新步骤版本；旧客户端即使用新revision也不能复用旧步骤版本。工程输入发生变化则返回input_stale，需要新的执行决策。刷新等待记录不是自动确认。

确认复核真实证据：几何验证、Mesh OK/网格输入、成功求解对应输入/场、当前成功后处理与报告门禁。测试专门让“网格作业退出0但没有网格证据”，C确认仍被拒绝，case保持B。完整C/D/E真实数值闭环继续由已有CAE基准验证，N3会把专业证据收尾全部纳入适配器。

## 取消、事务与未完成边界

`request_cancel` 持久化run取消并取消未执行节点；活动Job取消与attempt.cancelling在同一控制事务记录。worker心跳可观察取消并停止本地父进程；实际退出/收尾后才把工作流完成为cancelled。已完成步骤保留成功记录，不被重写成取消。当前仍不是N4要求的完整Windows后代/WSL组/MPI ranks停止，不能把这个测试当作全进程树验收。

共享case控制锁保证本调度服务的并发顺序；普通文件/Agent/原生求解器写入尚未全部纳入N2协调器。caseDict文件、工程事件文件和数据库不能共用事务：文件写入后DB失败仍需N2提交日志与N5恢复，不能宣称文件与DB原子提交。API创建run后的队列投递缺口、所有者重启对账、产物代次、未知shell/外部资源自动重放策略继续保留在后续任务。

## 验收证据

`workflow_scheduler_smoke.py --postgres` 在独立SQLite和随机命名临时PostgreSQL分别通过20项断言，使用真实CadQuery子进程、真实JobProcess和实际FastAPI ASGI请求（真实JWT及case权限）；没有生产测试开关。

| 范围 | 证明 |
|---|---|
| 计划/提交 | 5种目标图、V退出限制、6并发同键一run、不同请求409、创建回滚不留下steps/事件 |
| 调度/执行 | 未完成依赖拒绝、并发排队一个attempt、真实CadQuery与finalizing屏障、重连不重算、完成事务关联run/step |
| 确认 | 归属/明确确认/目标/步骤版本、revision改变失效、刷新后版本变化、双确认只有一次推进、只释放下一依赖、退出0无网格证据仍拒绝 |
| 失败/取消 | 原生退出7后全下游blocked不启动、持久取消/中断不重排、运行worker通过心跳观察取消、输入变化拒绝新步骤 |
| 原子性 | 排队回滚不留Job/attempt；注入终态commit失败后Job仍running、attempt/step仍finalizing、run仍running，完成事件全部回滚；序号连续、一事件一outbox |

最新证据：`tmp/workflow-scheduler-rxtxc6u6/result.json`。两种数据库均实际通过，临时PostgreSQL已删除。补充兼容领取双数据库13项：`tmp/job-claim-uuplqohj/result.json`。

本轮回归：`tmp/mainline-N1-scheduler-evidence/`中的offline19/19、core14/14、execution3/3、真实CAE3/3及阶段兼容2/2。Python入口177，未执行全集；前端/浏览器仍为此前证据。已有开发PostgreSQL未迁移，旧开发API未重载。源码归档`platform/backups/mainline-N1-scheduler-20261102.zip`及其SHA-256旁文件用于本里程碑复核，恢复到新目录。

本页N1验收时下一单元为N2-01；当前N2-01已完成，其他N2任务与注册几何处理器也已有增量，最新下一单元见主线台账。N1-04/05/06剩余项继续保留；任何完整故障场景和整项FF尚未因此关闭。
