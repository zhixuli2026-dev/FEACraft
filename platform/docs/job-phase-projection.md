# 作业公开phase与跨API一致性

2026-11-12，N6-06第1组。主线42/53、剩11；本项其余刷新选择、日志游标、报告版本与完整历史阅读继续，全部53/295保持。

## 契约与修复

作业`status`保留原状态与完成证据门控。公开`phase`统一来自持久化attempt；旧记录无attempt时使用原Job状态。记录成功但完成元数据待收尾或无法核验时，公开状态仍为running、phase为finalizing。进程内的queued/starting/running/completed生命周期继续用于原执行器，但不再覆盖公开phase。独立的completion_error诊断保持，不据此制造或修改持久结果。

`job_execution.public_phase`服务列表、详情、提交和幂等回读共用的`_job_out`；删除详情接口仅在没有本地进程时覆盖phase的分支。作业/尝试/提交身份、取消、原生停止、日志和发布逻辑未变。

## 实际反例与验收

入口`server/job_phase_projection_acceptance.py`只导入已审查的testsupport生命周期辅助，不导入历史模型验收main。两个实际API共享独立SQLite/FS；实际停止恢复投递扫描任务以隔离官方本地POST的执行归属，未替换claim、进程、结果或状态函数。三个实际Python进程分别成功、退出7、运行中被第二个API取消；无模型或CLI推理。

- 修复前`tmp/job-phase-projection-fqw7_sn7/phase-result.json`：反例成立，但passed保持false。原API的列表/详情均completed，另一个API分别succeeded、failed、cancelled。三个真实进程停止和两个API退出已核验。
- 修复后`tmp/job-phase-projection-_w4brd7u/phase-result.json`通过：列表/详情、两API同键原Job回读及原API退出后读取均保持succeeded/failed/cancelled，原attempt ID和失败/取消原因保留。Job、attempt、submission-request全部行哈希不变，未重复创建或执行。
- 两种明确SQL故障仅在真实成功进程已经停止后临时改写completion字段：pending及invalid均使两个API保留running/finalizing；原始JSON精确恢复且完整行哈希保持。不冒充真实发布中断或真实收尾失败。

本组实际模型0、模拟LLM0；实际OS/HTTP/SQL和明确元数据故障分别记录。没有运行历史混合模拟模型回归。当前实测FS/SQLite，不宣称本组新增PostgreSQL/S3、真实owner崩溃或完整故障矩阵验收；这些保留原后续范围。

冻结审查：`tmp/N6-job-phase-review-before-20261112.json`及`tmp/N6-job-phase-review-fixed-20261112.json`；源码/退出/状态对照封存见`tmp/mainline-N6-06-phase-projection-evidence/validation.json`。
