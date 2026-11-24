# 几何输入版本、产物发布与当前版本读取

2026-11-06 N3-02按原几何预览/导出与当前验证条件完成：当前双库工作流各17项、发布各16项、S3参数/体积/STEP及独立API5项、旧几何上下文/validation两套通过。主线27/53完成、剩26（8进行中/18待开始）；下一项N3-03网格质量和封存证据，原生双库验收正在运行。完整CAD功能及N4-N7仍保留，详见[N3-02验收](N3-02-acceptance-audit.md)。

2026-11-02后续增量：原几何preview/export/自动预览与新增工作流使用注册处理器，发布还校验本次execution_id及worker哈希；工作流B确认绑定本run生产版本。见 [registered-workflows.md](registered-workflows.md)。

2026-11-02。本次接通默认 CadQuery 自动预览、显式预览和导出。它是 N2-03/N2-04/N2-05 的首个专业流程增量；全工作流、网格/求解/后处理/报告迁移和恢复仍待完成。全部范围继续由 [主线计划](next-stage-mainline-plan.md) 和 [全功能计划](full-function-delivery-plan.md) 管理。

## 提交与输入

`POST /api/v1/cases/{id}/geometry:preview` 和 `:export` 均使用已校验归属的 case 控制锁，封存 `0-caseDict/caseDict` 和 `A-geo`，在独立 attempt 的 `run/` 执行。保存 `geometry.py` 触发的自动预览使用同一流程。输入副本、配额、取消及目录安全规则沿用 [attempt-workspaces.md](attempt-workspaces.md)。

可选 `Idempotency-Key` 使用已有 1–128 个可见 ASCII 字符契约。同用户/case、同键同模式回放原 Job，不重新分配 attempt 或发布代次；同键换模式返回409。未传键的请求各自创建新尝试。幂等键代表一次请求，编辑输入后希望重算须用新键或不传键。

预览和导出共用 `geometry` 发布类别。每次提交在 DB 分配递增 generation，并在 attempt 中记录 publication ID、head version、模式和输入描述。依赖描述包含标准化工程参数指纹及 `A-geo` 输入文件的路径、大小、SHA-256 和可执行标记。工作流状态、注释等记账变化不使几何过期；脚本、声明范围内的资产和工程参数变化会使成果过期。

标准生成文件、`A-geo/exports/`、日志、缓存及此前记录的生成路径不当作新几何依赖。执行仍复制整个 `A-geo`，保留已有脚本调用方式。这不是任意 Python 程序的完整依赖分析：外部路径、网络数据、把生成目录作为输入的自定义脚本需要后续显式依赖契约，不能以当前快照保证其可重放。

## 产物与事务边界

worker 两种模式均生成 scene、validation 和实际工具链记录（Python、CadQuery、OCP 版本及 worker 文件 SHA-256）。export 另生成 STEP/STL 和 manifest，并保留自定义 export hook、inline export 的新增/变化产物；用户输入脚本和资产不会作为输出覆盖原件。

执行器捕获输出清单后，发布器验证几何结果与封存脚本/工程输入匹配，并核对选中输出的字节数和 hash，然后准备目录：

```text
case/.vibeflow/publications/{publication UUID}/
  manifest.json
  files/A-geo/scene.json
  files/A-geo/validation.json
  files/A-geo/toolchain.json
  files/A-geo/geometry_manifest.json  # export
  files/A-geo/exports/...            # export
```

先写私有 `.preparing-{UUID}`，再用无覆盖目录移动发布。manifest 绑定 case、attempt、发布类别、代次、输入描述和输出清单，其 SHA-256 保存到 DB。普通文件、Agent 文本、上传和删除接口拒绝写入 `.vibeflow/publications`，沿用 `input_sealed`/409。

在同一 case 锁及 DB 事务中核对发布代次与 head version：

| 情况 | Job/发布行为 |
|---|---|
| 最新代次，输入仍匹配，验证通过 | publication committed、活动 head、Job/attempt succeeded 及事务事件一同提交 |
| 被较新提交或手工编辑淘汰 | 保留有效计算结果为 superseded 历史版本，不替换当前 head |
| 最新代次，但当前输入已改变 | Job failed / input_stale，不切换 head |
| 验证、清单或目录准备失败 | Job failed / publication_failed，不报告成功 |
| bundle 已生成而 DB commit 失败 | 不报告成功，保留旧 head；新目录及 prepared 控制记录留待对账 |

DB 提交后才尝试更新旧 `A-geo` 文件路径。再次取得 case 锁且确认该版本仍为当前 head 后才写兼容文件，旧任务晚完成不会覆盖新 head 或新文件。兼容文件写入失败保留权威版本及成功终态，并尝试持久化 `artifact.alias_repair_required`；该附加通知也失败时记日志，不把已经提交的成功改成失败。

自定义导出钩子生成错误 JSON 类型（例如将 validation 或 scene 改为数组）会被专业产物校验明确拒绝，任务以 `publication_failed` 结束并保留旧 head，不会因属性访问异常一直停在 finalizing。

这里没有声称文件系统与数据库构成单一原子事务。未知提交结果、孤立目录、自动修复兼容文件和重启恢复需要 N2-06/N5 的日志与对账机制。

## 读取、历史和手工编辑

| API | 行为 |
|---|---|
| `GET /cases/{id}/geometry/scene` | 按 DB head 读取并校验不可变 scene，返回 publication 元数据 |
| `GET /cases/{id}/geometry/validation` | 同一 head 下读取验证结果；只有草稿而无活动成果时返回409 |
| 上述接口加 `?publication_id={UUID}` | 在 case/用户归属校验后读取指定历史版本；current 与 stale 分开表达 |
| `GET /cases/{id}/geometry/publications` | 最新50条发布记录，包含代次、状态、attempt、是否当前、目录和 manifest hash |

新版本元数据包含 `id/generation/status/current/stale`。当前表示被 head 选中；过期表示其输入与当前输入不匹配。历史版本不会仅因不是 current 就无法阅读。没有发布控制记录的历史 case 保留原读取行为；手工改写生成别名后显示草稿，不把旧验证结果当作当前成果。

受协调的 storage 写入/删除、Agent 文本写入、分块上传完成和 CAD WebSocket 场景修改会记录被改路径。修改当前生成文件时清空活动 head、增加 version 并记录失效事件；等待中的旧生成任务也会被 version 检查阻止发布。修改普通几何输入由指纹检查标记过期。重新预览/导出创建新版本，保留历史。

显式 A→B 推进使用当前已发布 validation 和输入指纹，仍要求用户确认和正确 caseDict revision。兼容文件缺失不妨碍读取权威证据。场景能显示不等于工程验证通过，草稿、过期或损坏的验证不能推进。

## 验证与交付边界

专项脚本为 `server/geometry_publication_smoke.py`，使用两个独立 API 进程、共享临时 SQLite 或本机临时 PostgreSQL、独立 fs 和真实 CadQuery。故障注入仅存在于临时测试应用，没有生产调试开关。结果与回归记录保存在 `tmp/mainline-N2-geometry-publication-evidence/`；最终证据为 `geometry-publication-result.json` 双库各16项、`attempt-workspace-result.json` 双库各23项、`case-write-result.json` 双库各93项，core14/14、writes30/30、CAE3/3、offline定向5/5。临时PG已删除，开发库未迁移/API未重载；未重跑完整offline、184个Python入口或前端。状态以台账为准。

兼容性验证覆盖几何构建返回形式、Context 参数、preview 不调用 export hook、显式导出 hook/inline STEP、拓扑验证及 STEP 导入。新增四套几何兼容脚本已改为隔离 API 和独立算例，纳入 writes profile。

在项目根使用已安装依赖的解释器复验：

```powershell
& platform/server/.venv/Scripts/python.exe -X utf8 -B platform/server/geometry_publication_smoke.py --postgres
& platform/server/.venv/Scripts/python.exe -X utf8 -B platform/server/attempt_workspace_smoke.py --postgres
& platform/server/.venv/Scripts/python.exe -X utf8 -B platform/server/case_write_smoke.py --postgres
& platform/server/.venv/Scripts/python.exe -X utf8 -B platform/tools/run_regression.py --profile core
& platform/server/.venv/Scripts/python.exe -X utf8 -B platform/tools/run_regression.py --profile writes
& platform/server/.venv/Scripts/python.exe -X utf8 -B platform/tools/run_regression.py --profile cae
& platform/server/.venv/Scripts/python.exe -X utf8 -B platform/tools/run_regression.py --profile offline --only job_claim_smoke,job_idempotency_api_smoke,job_finalization_smoke,workflow_scheduler_smoke,backup_manifest_smoke
```

`--postgres` 限本机 PostgreSQL，创建并最终删除有唯一测试前缀的临时库；默认仅临时 SQLite。真实专业测试需要当前已验证的 CadQuery、WSL/OpenFOAM、TeX、Pandoc 和其他专业运行时。源码回退使用 `platform/backups/mainline-N2-geometry-publication-20261102.zip`，先校验再解包到全新目录；不直接覆盖代码、现有数据库或用户文件。通用恢复流程见 [mainline-baseline.md](mainline-baseline.md)。

当前未交付的部分必须保留：

- 下游网格/求解/后处理/报告尚未统一消费 publication 引用；原始文件读取接口仍可能看到逐文件更新中的兼容目录。当前保证针对几何 canonical/pinned 读取和 B 阶段证据，不能推广到全部消费者。
- 非几何任务仍须逐项接入隔离输入及不可变输出；整个依赖图的重算范围、工具链变化失效和调度器自动派发尚未完成。
- 尚无 publication 历史浏览器 UI、完整重试/续算/取消进程树、启动对账或 S3 多副本事务。
- case/workspace 复制及备份v1排除 attempts/publications，避免移植带旧身份的执行记录；备份v1仍不含工作流控制表，不是完整执行灾备。
- 本机任意代码仍能访问 cwd 外部，未实现原生沙箱；应用写保护与 hash 检查不能替代操作系统隔离。

后续 Job/内部步骤队列已支持显式绑定发布版本、混合输入封存及失效检查，见 [publication-inputs.md](publication-inputs.md)。其他专业处理器和完整依赖图仍须继续接通；补提交日志/孤立目录/兼容文件恢复，再完成旧路径及前端历史体验。没有以本次几何局部验证关闭整项 FF 功能。
