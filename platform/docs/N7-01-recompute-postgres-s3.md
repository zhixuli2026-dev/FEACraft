# N7-01 PostgreSQL 与 SeaweedFS 的重算和重启

2026-11-19。补验 N2-04 交给 N7-01 的双后端重算组合，覆盖 V06 相关的原输入变更、依赖失效与重算。算例使用真实 CadQuery、OpenFOAM `blockMesh` 和 `checkMesh`。原 N2-04 专项的双参数分支分别运行 PostgreSQL/文件系统和 SQLite/SeaweedFS，本专项让同一次执行实际使用 PostgreSQL 和 SeaweedFS。[原重算范围](workflow-recomputation.md) · [主线故障矩阵](next-stage-mainline-plan.md)

## 确认 C 阶段时的兼容路径竞态

首轮真实组合完成两个几何作业，同键并发只创建一次重算。C 阶段首次确认收到 HTTP 500。堆栈显示读取当前网格的 S3 兼容文件时，生产目录返回 404，而 `ClientError` 未被证据校验处理。该轮在确认处中断，临时数据库及对象存储均已回收。[首轮结果](../../tmp/workflow-recompute-pg-s3-ybtn315x/result.json) · [原 API 日志](../../tmp/workflow-recompute-control-rb9tcqz2/api.log)

独立对照仅暂停已提交网格的真实别名写入。直接从 SeaweedFS 读取了不可变发布及 `B-mesh/boundaries.json`，数据符合 PostgreSQL 指针和文件摘要。此时生产目录的同名兼容文件为 404，原 C 请求实际返回 500。放行原修复后，发布状态为 `settled`，`alias_error` 为空，同名兼容文件与不可变文件 SHA-256 一致。对照随后成功完成 C 和冷 API 重启，保留早先 500 作为原行为记录。[同一对象的前后回执](../../tmp/workflow-recompute-control-9zxtxp3b/mesh-alias-window.json) · [旧行为及重启证据](../../tmp/workflow-recompute-pg-s3-m2fca7kx/result.json)

C 门禁现在在兼容文件同步期间给出 HTTP 409 和可重试原因。同步完成后仍校验发布、输入与几何依赖、兼容文件的内容。S3 的缺失对象折算为原文件证据缺失，保留对应相对路径，其他 S3 错误继续沿原错误路径传播。[C 阶段门禁](../server/app/services/mesh_publication.py#L81) · [文件证据校验](../server/app/services/case_evidence.py#L78)

## 最终实测

- 等待同步时的 C 请求返回 `409 / evidence_invalid`，回执明确提示继续等待。同步完成后，在隔离 S3 目录移去一个兼容文件，C 请求仍返回 HTTP 409 并指出 `B-mesh/boundaries.json`。从已提交的不可变版本恢复完全相同的字节后，C 确认正常通过。[两类冲突和正常同步](../../tmp/workflow-recompute-control-2qjocpvb/mesh-alias-window.json)
- 算例文件与数据库的阶段不一致，以及重算规划期间外部改写字节，两次均返回 `input_stale`，外部字节保持，未创建额外运行。同键并发创建一个重算，复用原几何成果，后续仅发起一次网格作业。整轮共有 2 个真实 CadQuery 作业和 1 个真实 WSL 网格作业。网格的宿主与 WSL 范围均经独立探测确认退出，持久停止回执已核验。[14 项断言和实际范围](../../tmp/workflow-recompute-pg-s3-9wk31930/result.json)
- 原 API 按测试故障点退出，替代 API 使用空本地缓存从同一数据库和对象存储读取。原工作流、重算、网格结果和发布头一致。同键重放后 PostgreSQL 的 3 个 Job、3 个 attempt、2 个工作流、174 条事件的摘要保持一致。SeaweedFS 上的网格点、`blockMesh.log`、`checkMesh.log` 和不可变发布清单逐字节复核。另一账户重算返回 HTTP 404。[冷启动与实际 S3 对象](../../tmp/workflow-recompute-pg-s3-9wk31930/result.json)
- 两个准确 API 出生身份均核验退出。通过 `pg_database` 确认临时库已删除，并通过 WSL `/proc` 独立确认 SeaweedFS 已退出。模型调用与模拟模型调用均为 0。[独立资源回收](../../tmp/workflow-recompute-pg-s3-9wk31930/cleanup-independent.json)

## 复验入口

默认模式启动独立临时 PostgreSQL、SeaweedFS 和两个先后运行的 API，替代 API 使用新的本地缓存。`--observe-legacy-gate` 用于旧实现的 500 对照。入口在启动前核验全局 `no_llm` 审查登记，含调用的测试辅助模块、应用源码及数据库迁移。[显式入口](../server/workflow_recompute_pg_s3_acceptance.py) · [复用的控制专项](../server/workflow_recompute_control_smoke.py) · [回归审查规则](../tools/regression_llm_policy.py)

```powershell
& 'platform/server/.venv/Scripts/python.exe' -B -X utf8 `
  'platform/server/workflow_recompute_pg_s3_acceptance.py'
```

物理跨主机掉电、断网及接管仍按 N7-01 的原矩阵验收，需要独立物理宿主。[执行主机证据边界](remote-worker-routing.md)
