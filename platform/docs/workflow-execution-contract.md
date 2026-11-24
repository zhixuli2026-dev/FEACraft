# 工作流执行契约 v1（N0-03 / N0-05）

日期：2026-11-02。此文为执行契约；实体迁移、状态机、Job事务事件和实际claim/attempt基础已实现（现状见 [job-execution.md](job-execution.md)），证据见 [workflow-persistence.md](workflow-persistence.md)。首批6个几何工作流API已经实现，实际请求/限制见 [registered-workflows.md](registered-workflows.md)；本文其余目标设计（尤其resume、专用events、多专业链路）仍待实现，不能据设计示例调用。现有路由事实见 [入口清单](execution-entry-matrix.md)，任务与验收见 [主线计划](next-stage-mainline-plan.md)。

2026-11-02执行增量：显式输入清单的独立attempt目录已接入Job/内部工作流队列/执行器和受限shell重试，详见 [attempt-workspaces.md](attempt-workspaces.md)。专业handler、全部入口迁移、发布与恢复仍按本契约推进。

## 请求和归属

新增 `POST /api/v1/cases/{case_id}/workflow-runs`，主体取已认证用户及 case 所属工作区，不信任请求中的 user/org。请求包含 `target`（geometry/mesh/solve/postprocess/report）、`expected_revision`、`run_dir`、受限 handler 参数与预算；不接受客户端任意依赖图或任意 handler import 路径。`Idempotency-Key` 为 1–128 个可打印 ASCII 字符，唯一范围为 case+发起用户+key。服务计算规范化请求 SHA-256：相同 key/相同请求返回原 run，相同 key/不同请求返回 409 `idempotency_conflict`。没有 key 时生成新 run，不能假定重复点击可安全去重。

示例请求（设计目标）：

```json
{"target":"report","expected_revision":12,"run_dir":"C-run/case-1",
 "parameters":{"geometry_mode":"preview","report_formats":["html","docx"]},
 "budget":{"timeout_seconds":3600,"max_log_bytes":16777216}}
```

服务先验证目标、路径、预算、输入版本和配额，再同事务写 run/steps 和 outbox；排队后才由 worker 原子 claim。非法请求没有 Job/进程/输出副作用。支持读取 `GET /cases/{id}/workflow-runs`、`GET /workflow-runs/{id}`（steps+attempts）、`POST /workflow-runs/{id}:cancel`、`:resume` 与 `/events?after=`。所有 ID 均回查 case 权限；保持既有无权资源的 404/403 约定，不回显另一用户的幂等请求。API 示例不包含服务器任意绝对路径、密钥或完整命令日志。

## 数据结构与不可变性

| 表/实体 | 必须字段、索引及约束 |
|---|---|
| workflow_runs | id、case_id FK、org_id FK、created_by FK、target、status、version、request_hash、idempotency_key（可空）、expected_revision、run_dir、plan_json、input_manifest_json、budget_json、cancel_requested_at、error_code/detail、created_at/updated_at/ended_at；unique(case_id,created_by,idempotency_key)，case+created_at 索引。org由服务解析授权上下文，不能信任请求任意org |
| workflow_steps | id、run_id FK、step_key、handler/version、status、version、dependencies_json、confirmation_state/revision、input_manifest_json、output_manifest_json、active_attempt_id（可空）、error_code/detail、时间；unique(run_id,step_key)。active_attempt 在服务检查归属，避免循环建表依赖 |
| job_attempts | id、job_id FK（每次重试新建Job，旧Job保留）、root_job_id FK、retry_of_attempt_id FK、step_id FK（可空：兼容单 Job）、number、phase、version、worker_id、claim_token、lease_expires_at/heartbeat_at、process_identity_json、cancel_requested_at、exit_code/reason_code/detail、input/output/publication_json、时间；unique(job_id,number)与unique(root_job_id,number)，worker+lease 索引。claim_token 是随机 fencing token，不是授权 token，不在公开响应回显 |
| artifact_publications | id、case_id FK、kind、generation、attempt_id FK、status、immutable_path、manifest_sha256、committed_at；unique(case_id,kind,generation)。发布 head 单独存 unique(case_id,kind) 行（next_generation、active_publication_id、version），不能只靠内存最新 Job |
| durable_events | id（稳定 UUID）、case_id FK、seq、run_id/step_id/attempt_id（可空 FK）、type、payload_json、created_at；unique(case_id,seq)，按 case+seq 补拉；seq 由 case event counter 原子递增 |
| event_outbox | id、event_id unique FK、status、claim_token/lease、attempts、next_attempt_at、last_error、delivered_at；同状态事务插入，独立投递，at-least-once，消费者按 event_id 去重 |

JSON 均版本化，输入/output manifest 有 schema、相对路径、字节数、SHA-256、工具链版本和来源。禁止将 credential、JWT 或远程私钥持久化进 manifest/event。DB 为控制事实，case 文件为工程事实；读 DB 镜像不能覆盖当前文件。重试保留旧 attempt/日志和终态。事件 seq、attempt 编号和 publication generation 由 DB 条件更新/唯一约束分配，不能 `max()+1` 后裸写。

## 状态转移与竞争

每次状态更新要求 expected version 或旧状态条件，影响行数必须为 1。终态 succeeded/failed/cancelled/interrupted 不允许回到活动态；重复相同事件可幂等返回现有值，冲突终态返回 `state_conflict`。恢复创建新 attempt/运行动作，不修改旧终态。

| 范围/当前状态 | 允许的后继 |
|---|---|
| run queued | running、cancelling、failed、cancelled、interrupted |
| run running | awaiting_confirmation、cancelling、succeeded、failed、interrupted |
| run awaiting_confirmation | running、cancelling、failed、interrupted |
| run cancelling | cancelled、failed、interrupted；已提交成功的竞争返回实际 succeeded，不伪造撤销 |
| step pending | ready、blocked、cancelled |
| step ready | running、awaiting_confirmation、cancelled、blocked |
| step running | finalizing、failed、cancelling、interrupted |
| step finalizing | succeeded、failed、cancelling、interrupted |
| step awaiting_confirmation | succeeded（证据+显式确认）、cancelled、blocked、interrupted |
| step cancelling | cancelled、failed、interrupted |
| attempt queued | claimed、cancelled、interrupted |
| attempt claimed | starting、cancelled、failed、interrupted |
| attempt starting | running、cancelling、failed、interrupted |
| attempt running | finalizing、cancelling、failed、interrupted |
| attempt finalizing | succeeded、cancelling、failed、interrupted |
| attempt cancelling | cancelled、failed、interrupted |

内部 attempt phase 的 claimed/starting/running/finalizing/cancelling 对旧 Job 映射 running（queued 仍 queued）；公开附加 phase/reason。纯计算退出码 0 只进入 finalizing。专业校验、输入复核、必要同步、发布结果和 DB 终态确定之后，查询才显示 succeeded、日志 SSE 才发送 done、队列才确认完成。可选全文索引失败不改变专业终态。DB 关键提交失败必须向等待方报错，不能靠内存 succeeded 掩盖。

持久取消先写 cancel_requested_at，再发送可丢失的唤醒。worker claim 前、spawn 前、专业收尾前、活动指针提交事务内都检查；重复取消幂等。发布事务获胜之后收到取消，返回既有终态。进程退出并不等于所有 descendants 退出，必须基于宿主 PID+创建时刻、进程组/WSL 身份和 claim token 判断。PID 无法验证时保留需要介入，不能猜测性误杀。

错误码固定为 `invalid_request`、`revision_conflict`、`input_stale`、`idempotency_conflict`、`state_conflict`、`dependency_failed`、`confirmation_required`、`process_start_failed`、`process_exit_failed`、`process_cleanup_failed`、`evidence_invalid`、`sync_failed`、`publication_superseded`、`publication_failed`、`persistence_failed`、`resource_limit`、`cancel_requested`、`lease_expired`、`recovery_required`、`runtime_unavailable`。保留人类可读 detail 和可重试标记；superseded 是计算成功但未成为当前版本的 publication 结果，不强制改成求解失败。`process_cleanup_failed` 表示原生退出/所属范围或控制完成报告无法核验，不能登记成功，详见 [process-tree-stop.md](process-tree-stop.md)。

## 步骤、确认与失效

服务生成 DAG：preflight → geometry → confirm_B → mesh → confirm_C → solve → confirm_D → postprocess → confirm_E → report。目标决定截断位置，已有证据必须复核后才能 reuse。工程阶段 A–E/V 与 run 状态分离；确认节点只允许使用已有 `/caseDict:advance` 的 confirmed、expected_revision、顺序和证据门禁。工作流创建与 Agent 工具执行不隐含阶段确认。

上游失败时下游 blocked，不能执行。几何/网格/物理与运行输入变更使对应依赖下游 stale；workflow/errors/provenance/status 等记账不能无故使工程指纹变脏。每个 attempt 有独立输入/运行/输出目录，封存 0/0.orig、网格和重启场，记录相对映射；不删除用户原件。只读当前报告按活动版本解析，同一次读取固定一个 generation；兼容别名是镜像，不是活动事实源。

新 attempt retry 从冻结输入重新开始；rerun 采用当前输入并生成新 run；resume 需 handler 明确支持、验证 solver/mesh/start time/field/checkpoint。未知 shell、审批工具、远程资源创建/计费均不自动重放。spawn 成功但未登记时依据启动票据/进程身份对账，无法证明归属则 interrupted+recovery_required，禁止再开同一个求解器。

## 持久事件与文件提交

事件 envelope：`{schema:"vibeflow.workflow-event.v1",event_id,case_id,seq,type,run_id,step_id,attempt_id,payload,created_at}`。最少类型 `workflow.created/state_changed/confirmation_required`、`step.state_changed`、`attempt.state_changed/cancel_requested/recovery_required/process_stopped`、`artifact.published/superseded`。状态、seq 与 outbox 同一 DB 事务，cursor 表示最后收到 seq。保留期以明确的 `cursor_expired` 和当前快照返回，不能无声丢历史。

fs 与 DB 不共享原子事务。生成不可变目录和 manifest → fsync 提交日志 → DB 写 publication/head/outbox → 兼容别名修复 → 标记日志完成。每处崩溃均按日志+哈希+DB head 对账，既不盲目覆盖活动版本，也不删除唯一有效产物。迟到旧 generation 不可切换 head。S3 使用对象版本/条件写和 staging 冲突协议，不能用 fs rename 假装跨副本提交。

## 兼容、部署和阶段退出

首个交付范围为本机 fs、一个 API 协调者、受控本地/WSL workers；SQLite 仅隔离测试，正常开发 PostgreSQL。新增表通过 Alembic，在临时 SQLite/PostgreSQL 验证 upgrade/downgrade/re-upgrade；不在本阶段操作现有开发/生产库迁移。旧 Job 行与 API、日志、批次文件继续可读；旧历史没有 attempt 时显示 legacy，不虚构输入、worker 或恢复资格。

Redis/arq 必须重用相同 claim、租约与完整完成通知，不把通知广播当持久取消。S3、跨主机/多副本、HPC 和组织外部资源的后续交付由 [全功能计划](full-function-delivery-plan.md) 的 R6/J14/J15/I 系列承接；首个单实例版本不因此删除这些任务，也不在 UI 宣称支持未验证部署。

退出要求仍为 N7 全部门禁：53 项证据、V01–V24 故障矩阵、旧三条真实 CAE、所有接入入口回归、临时 PostgreSQL 迁移/恢复、浏览器刷新/确认/取消/恢复、资源预算、部署/回退文档与可恢复版本包。未满足上述条件不能将整个阶段或295项功能标记完成。
