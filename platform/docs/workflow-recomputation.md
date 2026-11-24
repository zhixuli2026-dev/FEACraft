# 按依赖范围创建重算流程

2026-11-20，显式回到 A 的字典写入改为锁外暂存。新流程、依赖绑定、修订镜像和文件见证仍共同提交。本地和认证 SeaweedFS 均通过 40 MiB 历史并发，本地通过提交前后实际退出。PostgreSQL 和 SeaweedFS 的重算、网格发布及冷重启也完成复验。[重算与迁移事务验收](file-transaction-diagnostics.md#重算与旧计划迁移)

2026-11-05，N2-04 实际重算实现。与只读[依赖图](dependency-invalidation.md)配套，新增 `POST /api/v1/workflow-runs/{run_id}:recompute`。请求从一条已终止的注册工作流创建新流程，保留旧流程、旧尝试和不可变成果，不把历史成功改写成失败，也不复用旧尝试执行新的输入。

## 请求与返回

必须提供 `Idempotency-Key`，JSON 至少包含：

```json
{
  "expected_version": 8,
  "expected_revision": 12,
  "confirmed_reset": true
}
```

版本与 revision 应从当前 API 响应取得，上例数值只是结构示例。`confirmed_reset=true` 明确确认将算例工程阶段按既有接口回退到 A；未确认时拒绝。随后 B、C、D、E 仍使用原工作流逐级确认接口。创建请求不会隐式批准这些阶段。

可选 `target`、`run_dir`、`parameters`、`budget` 默认沿用来源流程。参数仍只接受已有 `geometry_mode=preview/export`、`report_mode=compose/markdown`；不能提交自定义 DAG、任意处理器或客户端提供的复用发布 ID。

返回新的工作流详情。新增字段：

- `recomputation`：来源流程 ID/版本、观察 revision/事件序号、复用发布的完整身份、实际重建步骤。
- `recomputation_reset`：确认用户、回退前后阶段及 revision。
- 每步骤 `execution_mode` 与 `reused_publication`：复用步骤没有新 JobAttempt，不能显示成一次新的原生计算。

同一用户、算例和幂等键的相同请求始终返回同一条新流程，即使该流程已经完成；不同载荷返回冲突，不重复回退阶段或创建作业。目标已有合适的当前成果时返回“无需重算”的 `409 state_conflict`，不会创建空流程。

## 范围与固定版本

从 geometry 到目标步骤选择仍有效、模式和 runDir 符合要求的连续上游成果。从第一个缺失/过期/模式不匹配的步骤开始，重建到目标的全部后续步骤。即使后续旧成果的输入字节暂未变化，新的上游发布身份也需要对应的新下游来源关系。

例：改变运行目录中的热扩散系数时，几何仍有效，新流程复用几何，重新执行网格、求解、后处理和报告。目标改为 mesh 时，不创建 solve、postprocess、report 步骤。几何变化且需要重新导出时，五个步骤均重新执行。只改变报告模式时，可以选择复用前四个生产者；实际报告准备仍必须通过该模式的输入检查。

复用记录绑定 publication ID、kind、generation、attempt ID 和 manifest SHA，阶段确认会重新验证该发布的完整来源及当前性。期间出现新的几何 head，不会把等待中的旧确认悄悄改绑到新版本。后续原生准入、输入封存和最终发布继续使用现有复验协议。

## 并发、异常与恢复

源检查在释放数据库事务后执行。最终创建前重新授权、取得算例控制锁并复验事件序号、head、数据库读集，以及 caseDict 的原始字节与阶段镜像。协调写入或规划期间直接修改 caseDict 都会拒绝创建。对于其他绕过协调器的外部文件写者，本操作不宣称提供跨文件原子快照；后续确认和原生执行仍必须复核真实输入。

同算例存在另一条活动工作流时，拒绝重算引起的 A 回退，避免改变另一流程正在使用的全局工程阶段。损坏或无法验证的发布先返回需处理状态，不能假装已有可复用成果。

新流程、步骤和审计先提交，再派发原生任务。若提交后、派发前请求中断，重试原幂等键得到已创建的流程，可以通过现有 `:dispatch` 继续，不再次回退或重新创建。本批测试在提交后注入 API 异常，并用另一 API 实例继续；这不冒充完整的机器宕机恢复矩阵。

A 回退继续使用现有 `engineering_stage.advance` 和 case 写入协调器。文件写入与数据库提交之间的崩溃补偿仍属于 N2-06；本功能不把两种存储的提交宣称为一个原子事务。旧未注册工作流计划仍需要 N5-06 的显式迁移；不会通过重算入口绕过注册契约检查。

## 验收范围

`workflow_recompute_smoke.py` 在真实 CadQuery、blockMesh/checkMesh、OpenFOAM、后处理和 Pandoc 上执行输入变化后的新流程，验证并发幂等、显式阶段确认、旧流程/报告保留、目标裁剪、取消、跨用户拒绝、报告模式选择、固定复用版本及另一 API 实例继续派发。默认 S3，`--fs` 使用文件后端。

`workflow_recompute_control_smoke.py` 默认使用独立 SQLite；`--postgres` 增加临时 PostgreSQL，`--s3` 增加 S3/SQLite。使用真实几何与网格作业检查并发创建、无新原生尝试的复用、重放、文件/DB 阶段不一致和规划期间外部字节改写。不会迁移开发数据库或重载端口 8000。

`workflow_recompute_plan_smoke.py` 验证五个目标的 15 种合法复用边界和 10 种非法计划；原调度器与原注册几何工作流继续保留兼容检查。

完整前端流程、旧计划/Agent checkpoint 的联动、跨主机与全存储故障矩阵、任意外部依赖与工具链的完整冻结继续属于原 N5/N6/N7 和全功能任务。N2-04 的后端依赖与重算实现不等于这些任务全部交付。

最终规范证据：`tmp/mainline-N2-recomputation-closure-evidence/`。完整文件/S3工程链各23项；最终入口控制fs+SQLite、fs+PostgreSQL、S3+SQLite各8项；计划25项，旧调度器20项及几何工作流17项。完整链之后只增加阶段镜像/caseDict字节准入复核，由最终三组合检查覆盖。初始控制夹具20×2×2网格未通过严格checkMesh，改用已验收的20×4×4；外部写入夹具的Windows换行转换改为精确写字节。失败日志保留，没有放宽产品校验。
