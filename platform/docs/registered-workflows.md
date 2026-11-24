# 注册步骤处理器与几何工作流

2026-11-11当前API状态：创建/分页/详情/取消/事件及v5显式恢复已接通，原输入重试、串行两次检查点和真实MPI/UDF重试已有实际证据；原API条件核对见[N6-03退出记录](N6-03-original-api-closure-audit.md)。主线40/53、剩13，完整页面整合仍由原N6-04～07继续；下面“resume待实现”等为早期阶段记录。

2026-11-11 N6-03增量：新增有case/筛选绑定的流程分页及单流程持久事件查询；损坏计划/事件/尝试关联返回明确409，变更前验证步骤、尝试和作业归属。SQLite/PG各5组实际检查、总计6个CadQuery作业及临时库删除通过，真实模型0、模拟LLM0。见[当前API和证据](workflow-api-queries.md)。真正的流程恢复/检查点resume仍待实现，见[原子续接设计](workflow-resume-design.md)，不将dispatch当resume。主线39/53、剩14，53/295目标保持；下文为早期阶段记录。

2026-11-06 N3-05后处理按原条件完成：SQLite/PostgreSQL各21项真实热传导提取与重算、参考对比、图件/指标防伪、来源变化、取消与迟到结果通过；进程退出0，临时数据库已清理。当前主线30/53完成，剩23（5进行中/18待开始）。N3-06报告各入口继续当前双库验收，N4-03机器入口清单已刷新但运行时验收尚未关闭；295项范围完整保留。

2026-11-06 N3-03网格与N3-04串行求解按原条件完成：真实网格双库各23项、求解双库各25项，本地文件计划7项、S3条件写入14项及旧后端拒绝1项通过。修复求解旧输出清理回归，并调整与排空约束冲突的迟到结果测试；未放宽生产校验。主线29/53完成，剩24（6进行中/18待开始）。下一步N3-05后处理、N3-06报告。全功能295项与N4-N7继续，正式SIWC联调仍暂缓。

2026-11-06 N3-02按原几何预览/导出与当前验证条件完成：当前双库工作流各17项、发布各16项、S3参数/体积/STEP及独立API5项、旧几何上下文/validation两套通过。主线27/53完成、剩26（8进行中/18待开始）；下一项N3-03网格质量和封存证据，原生双库验收正在运行。完整CAD功能及N4-N7仍保留，详见[N3-02验收](N3-02-acceptance-audit.md)。

N3-01按原统一接口与拒绝shell伪造证据条件关闭：九处理器共用契约，当前真实CadQuery双库各15项通过，并核对原专业验收与新发布恢复。N3-02后续仍受原N2前置约束，N4/N5未交付内容及295项范围全部保留。主线19完成/14进行中/20待开始，剩34；295项严格完整交付仍0。 详见[原条件收口核对](mainline-acceptance-review.md)。

N3-06第四批增量：普通HTML/DOCX与独立compose已统一注册发布，移除旧内存latest/直接写稿分支；保留旧证据门禁、覆盖和原字节备份，补齐固定生成时间、两代幂等键、历史和跨API验收。普通文档双库各23项、工程report各23/compose各25、core14/契约4/最终兼容3通过，见 [ordinary-document-workflows.md](ordinary-document-workflows.md)。主线仍9完成/14实施中/30待实施，295项功能范围不变；下一单元N4-01进程身份，N3-06完整取消/恢复前置尚未关闭。

处理器可选内部publication_channel(descriptor)为主文件派生持久类别；kind_for在绑定、发布和消费时统一核对。可选validate_publication(db,case,descriptor)在当前发布/读取复核DB工程门禁；两者仅来自静态注册表。LaTeX以此替代进程内latest并阻止失效后处理回退。

N3-06第三批增量：全部LaTeX入口已接通注册编译、动态资源封存、按主文件持久版本、PDF/SyncTeX/BBL内容验证与历史源码定位，双库各32项通过，见 [latex-workflows.md](latex-workflows.md)。HTML/DOCX各23项、compose各25项复验；主线仍9完成/14实施中/30待实施，295项功能范围不变。这一历史增量后的普通文档统一已由第四批完成；完整取消/恢复前置仍在N4/N5推进。

注册处理器可选实现内部before_publish(prefix,descriptor)与write_aliases(prefix,row,manifest)：前者在当前代次、源校验通过后且head提交前检查目标前置条件；后者接管提交后的兼容镜像并必须幂等。二者只能来自静态注册表，客户端描述符不能提供动态函数。compose用它保护人工稿和旧稿备份，既有几何/网格/求解/后处理/报告仍走原通用镜像。

N3-06第二批增量：独立报告草稿已接入注册双通道、原字节备份、覆盖保护、持久历史与HTML/DOCX生成来源，双库各25项通过，见 [report-compositions.md](report-compositions.md)。HTML/DOCX双库各23项复验；主线仍为9完成/14实施中/30待实施，295项功能范围不变。接下来继续LaTeX注册，N3-06整项未关闭。

report.build@1现已注册，工作流可在E确认后自动组稿并编译HTML/DOCX；parameters.report_mode可选compose（默认）或markdown。报告绑定同run已确认后处理版本，当前/历史与下载见 [report-workflows.md](report-workflows.md)。独立compose注册分支与全部LaTeX入口已由后续增量接通；无head普通报告路径仍待N3-06统一。

后处理已接通，工作流可执行至E确认；静态处理器新增 `postprocess.run@1`，绑定同run已确认solve版本，独立复算数值和图件，确认绑定本run后处理版本。详情见 [postprocess-workflows.md](postprocess-workflows.md)。报告HTML/DOCX、独立compose和LaTeX后续进展见本文顶部；普通报告兼容路径及完整取消/恢复仍待续。

串行求解已接通，工作流可执行至D确认；当前静态处理器另含 `solve.openfoam@1`。求解绑定同run已确认的网格生产版本，支持当前/历史求解证据查询，详情见 [solve-workflows.md](solve-workflows.md)。下方早期里程碑记录保留原验证范围。

后续网格接线已完成首轮验收：确认B后自动派发网格，准备失败返回workflow_dispatch.pending并保留已提交确认；同run只消费已确认的几何生产版本，C确认绑定本run网格版本。双库各23项见 [mesh-workflows.md](mesh-workflows.md)。本文下方原几何里程碑证据保留其当时范围。

日期：2026-11-02。本轮接通 N3-01 的统一接口、N3-02 的 CadQuery 预览/导出及 N6-03 的首批 API。当前真实执行链为预检 → 几何 → 等待 B 确认；后续网格处理器已注册，完整流程延伸到C确认，见 [mesh-workflows.md](mesh-workflows.md)；报告HTML/DOCX与求解/后处理后续接线见本文顶部。53 项主线、295 项全功能范围保持不变。

## 接口与执行契约

`services/step_handlers.py` 静态注册 `geometry.preview@1`、`geometry.export@1` 、`mesh.generate@1` 、`solve.openfoam@1` 、`postprocess.run@1` 和 `report.build@1`，契约名为 `vibeflow.step-handler.v1`。客户端不能提供命令、argv、任意步骤图或动态模块路径。已有几何预览、导出和自动预览 API 也使用同一处理器。

| 处理器职责 | 当前实现 |
|---|---|
| prepare | 检查 geometry.py、配置的 Python、预算和配额，记录源指纹及已有生成文件，构造固定 worker 命令 |
| validate_spec | 复核命令标识、运行时、worker 路径、参数和本次执行 UUID；不能用通用 shell 的退出 0 代替专业执行 |
| 输入与记录 | `PreparedStep` 定义输入选择、发布引用、命令、产物类别、描述；attempt 保存处理器版本、执行 spec 哈希及取消/恢复策略 |
| artifacts | 复用 geometry validation、scene、工具链和实际 STEP/STL 检查，依据封存文件校验产物 |
| source | 发布、读取和下游引用使用同一源指纹校验 |
| 取消 | 先持久取消请求，本进程持有 Job 时调用本地取消；其他 worker 通过现有心跳观察。完整进程树终止仍属 N4 |
| 恢复 | 不自动重放、不声称支持 resume；显式 dispatch 仅投递已经提交但尚未领取的任务，已领取任务不重复执行 |

注册工作流排队必须提供匹配的 PreparedStep；队列提交、领取和成功收尾检查持久绑定。缺失/不匹配的处理器记录不能降级成普通 shell。旧内部 `workflow-plan.v1` 队列接口为兼容现有调用继续保留；新的公共入口生成 v2 计划，dispatcher 不投递旧计划。

每次几何调用有独立 `execution_id`。worker 在 `toolchain.json` 写入此 ID、自身 SHA-256 和实际工具版本；发布时与准备记录核对。只复制上次成功的 scene/validation/toolchain，再运行一个退出 0 的进程，不能生成新成功发布。这是执行来源一致性检查，不是任意用户 Python 的隔离沙箱。

## 已实现的 API

以下路径均以 `/api/v1` 开头，并要求 case 的相应读写权限。

| 方法与路径 | 请求及行为 |
|---|---|
| POST `/cases/{case_id}/workflow-runs` | 创建/幂等复用并派发已注册计划；见下方请求 |
| GET `/cases/{case_id}/workflow-runs` | 最近 50 条运行摘要，按创建时间倒序 |
| GET `/workflow-runs/{run_id}` | 运行、计划参数、有序步骤、尝试、Job 状态、发布 ID/代次/状态及错误；不返回 claim token 或执行 argv |
| POST `/workflow-runs/{run_id}:dispatch` | `{expected_version}`；复核版本及可执行节点，使用已经持久化的 spec 原子领取 |
| POST `/workflow-runs/{run_id}:cancel` | `{expected_version}`；持久取消及可用的本地取消动作；重复取消返回现状，已结束记录不改写 |
| POST `/workflow-runs/{run_id}/steps/{step_id}:refresh-confirmation` | `{expected_version}` 使用步骤版本；只允许刷新同一生产版本的有效等待确认 |

```json
{
  "target": "geometry",
  "expected_revision": 0,
  "run_dir": "C-run/case-1",
  "parameters": {"geometry_mode": "export"},
  "budget": {"timeout_seconds": 3600, "max_log_bytes": 16777216}
}
```

`expected_revision` 必填且必须为非负整数；`geometry_mode` 默认 preview，可选 export。预算默认 86400 秒、16 MiB 日志，最大 86400 秒/64 MiB，日志最小 1024 字节。未知字段拒绝。`Idempotency-Key` 的作用域为 case + 发起用户；相同请求复用原 run，不同参数返回 409 `idempotency_conflict`。没有 key 的请求创建新记录。

计划延续既有阶段语义：A 阶段执行几何并等待 B；已到达目标阶段时只预检已有证据，不强制重算，也不会因为 geometry_mode 改为 export 就重新导出。需要单独重新计算时使用已有几何 preview/export API。V 阶段须先显式回 A。后续report.build已注册；未知处理器/版本仍返回409 recovery_required，未知目标由请求schema拒绝。`run_dir` 在这批纯几何任务中仅保存为规范化的计划参数。

提交先持久保存 run、steps、attempt、输入目录记录和发布预留，再启动原生进程；跨 API 重复投递受原子 claim 保护。提交后、启动前进程故障留下的 queued 任务可显式 dispatch。其他崩溃位置和过期租约恢复尚未全部实现，不能把 dispatch 当作 resume。

## 发布与工程确认

Job 成功只表示本次计算及其收尾成功。`geometry` 步骤成功后，工程阶段仍为 A；只有明确确认才能推进 B。

确认节点保存本 run 的生产步骤 publication ID，并要求该版本仍是当前、成功、源指纹有效的几何发布。它不会借另一条工作流的新结果完成本流程。旧计算迟到仍可保存为 superseded 历史成功，但其确认节点变为 blocked，run 失败。

确认仍调用 `POST /cases/{case_id}/caseDict:advance`：

```json
{
  "state": "B", "confirmed": true, "expected_revision": 0,
  "workflow_step_id": "详情中的 confirm_B 步骤 ID",
  "expected_step_version": 2
}
```

上述 revision/version 必须取当次详情，示例数字不是固定值。确认同时检查权限、caseDict 修订与原文哈希、步骤版本、工程输入、生产 publication 和原阶段门禁。仅记账修订变化可显式刷新等待记录；工程输入或生产版本变化不能通过刷新换成另一条流程的结果。确认 API 不自动启动尚未注册的下游步骤。

## 验证与剩余任务

`workflow_handler_smoke.py --postgres` 使用两个真实 API 进程、隔离文件目录、SQLite/临时 PostgreSQL 和真实 CadQuery。注入仅在测试临时模块内，生产没有测试开关。检查包括并发同键、真实导出、任意命令拒绝、缺失绑定、旧产物冒充、worker 指纹不符、跨流程确认、修订刷新、显式投递、迟到结果、运行取消、越权与读取端重启。

最终结果在 `tmp/mainline-N3-handler-evidence/`：workflow-handler双库各15项、geometry-publication双库各16项、publication-input双库各19项；core14/14、CAE3/3、geometry-compatibility4/4、compatibility-final4/4。原始来源见evidence-origin.json，开发库仍13表/旧head，临时测试PG已清理。未重跑完整offline/writes或前端。读取端重启检查发生在相关 Jobs 已结束后，不能代表运行 worker 的崩溃恢复。

- N3-01：接口及几何、网格、求解、后处理、注册HTML/DOCX/compose分支和全部LaTeX入口已接通；无head普通报告、其他专业入口与完整取消/恢复契约继续按原任务推进。
- N3-02：几何执行、产物发布和 B 确认已接通；完整前置任务与后续专业链路继续验证。
- N6-03：创建/列表/详情/取消/派发/刷新已接通；恢复决策、resume、工作流专用事件查询与 UI 仍待续。
- N2/N4/N5：完整依赖重算、所有产物类别、跨存储事务、进程树取消、崩溃对账与清理不因本轮通过而关闭。

本轮不修改数据库 schema，不迁移或重载开发实例。工作流表仍须按既有部署文档迁移到相应 schema 后使用。源码归档为 `platform/backups/mainline-N3-handler-20261102.zip`，仅恢复到新目录；不包含开发私有配置和业务数据。
