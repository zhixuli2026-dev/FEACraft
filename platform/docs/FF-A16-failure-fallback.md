# FF-A16 作业失败回退与溯源

作业失败事务记录 `workflow.failure_fallback_required`，绑定原作业、尝试、退出码、观察阶段和修订。回退先核验原进程已经停止、写入已经排空，并检查失败是否已被更新的成功发布取代。只在目标早于当前阶段时修改 caseDict。目标相同或更晚时追加一条失败时间线，修订号保持原值。

| 失败作业 | 目标阶段 |
|---|---|
| geometry | A 几何 |
| mesh | B 网格 |
| solve | C 求解 |
| postprocess | D 后处理 |
| convert、report | E 报告 |
| udf | V 开发 |

2026-11-19 复现了源文件与镜像不一致时的覆盖。隔离的真实子进程退出 29，SQL 观察为 V、修订 1，回退将已经变为 D 的磁盘文件覆盖成 C，并落下修订 2 和完成回执。修复后回退读取固定版本的 canonical caseDict，当文件缺失、无法解码、读取中变化或文件阶段与已提交镜像不同，保留待处理意图并报 `input_stale`。写入同一文件事务时继续核对所读取字节的 SHA-256。失败时间线新增独立 `exit_code`，与作业编号、类型、日志、目标和是否回退一起记录。原失败复现见 [修复前回执](../../tmp/fallback-authority-x3ikz6gg/failure-fallback-authority.json)。

## 当前验收

- 通过正式几何预览验证缺参回退。同一脚本先用 `geometry.parameters.required_width=4` 生成体积 24 的 CadQuery 实体。在 V 仅移除该参数后，实际 CadQuery 2.8.0 预览记录 `KeyError: 'required_width'` 并退出 1，停止和排空核验后回到 A。文件、SQL 和溯源从修订 3 同步到 4，一条失败事件关联原作业、日志和退出码。同一请求键复用原作业，回退重放保持文件不变，另一个账号的预览提交、回退重放及时间线读取均返回 404。[真实缺参回执](../../tmp/fallback-missing-cad-parameter-q5vjutup/missing-geometry-parameter.json)
- 用 14 个真实 Windows Python 子进程覆盖七类作业在 A 和 V 的策略。每个进程实际执行一次并退出 29，验证积压日志、停止和排空。初始 A 的七种失败只记录证据；V 的六种失败按映射回退，UDF 保持 V。核对磁盘时间线有唯一事件及结构化退出码，重复处理保持 caseDict 和时间线原样。测试是类型和故障协议，V 是主动进入的开发态。[七类作业矩阵](../../tmp/fallback-authority-g_8e_mqj/failure-stage-policy.json)
- 两个独立作业分别验证阶段漂移和读取后、提交前的实际文件变更。两次都收到 `input_stale`，SQL 保留 V 和原修订，磁盘保留期间的 D，溯源与时间线字节保持原样，且没有完成回执。第二个作业的文件事务确认拒绝了变化的 caseDict 路径。恢复基准字节后各完成一次 V→C，新增一个修订、两个时间线事件及对应溯源，原待处理意图与完成回执关联，重复处理保持新字节原样。[源文件与事务故障回执](../../tmp/fallback-authority-g_8e_mqj/failure-fallback-authority.json)
- 通过正式导入和作业 API，用含未结清错误的历史 D 算例验证显式推进被拒、真实 Windows 子进程退出 7 后 D→C，以及 convert 实际退出 9 后阶段保持 C。另一真实账号读取作业和时间线，以及调用夹具内的回退重放接口，均返回 404；所属账号复读原时间线不变。[历史导入验证](../../tmp/workflow-fallback-35jnyddz/result.json) · [账号权限验证](../../tmp/fallback-ownership-ry0glmww/fallback-ownership.json)

本轮用 D 盘隔离的本机文件存储和 SQLite，模型调用 0。生产修复在 `workflow_fallback.py`。新验收入口为 [workflow_fallback_authority_acceptance.py](../server/workflow_fallback_authority_acceptance.py)。此前的 PostgreSQL+FS、PostgreSQL+S3 验收另有真实 CadQuery 失败、三个 API 中断位置、恢复去重及真实 DeepSeek 工程联动。当前补验覆盖文件版本冲突和审计字段，继续引用已归档的多后端与 Agent 实测。[几何失败三处恢复](workflow-failure-recovery.md) · [真实工程与 Agent 恢复](engineering-recovery-acceptance.md)
