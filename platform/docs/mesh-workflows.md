# 注册网格步骤、质量政策与版本化证据

2026-11-06 N3-03网格与N3-04串行求解按原条件完成：真实网格双库各23项、求解双库各25项，本地文件计划7项、S3条件写入14项及旧后端拒绝1项通过。修复求解旧输出清理回归，并调整与排空约束冲突的迟到结果测试；未放宽生产校验。主线29/53完成，剩24（6进行中/18待开始）。下一步N3-05后处理、N3-06报告。全功能295项与N4-N7继续，正式SIWC联调仍暂缓。

2026-11-06 N3-03按原网格质量/边界封存条件完成：SQLite/PostgreSQL各23项真实blockMesh/checkMesh通过，进程退出0，临时数据库已删除。旧测试循环等待已修正，生产排空规则不变。当前主线28/53完成、剩25（7进行中/18待开始）；N3-04求解最终双库复验继续，N3-05后处理独立验收已启动。

串行求解增量补充：writeCompression=on产生的gzip核心网格文件只在独立尝试内解压规范化，再由checkMesh验证；本环境实际规范化points/faces/owner/neighbour，boundary原生为普通文件。输入controlDict不重写，工具链记录规范化文件列表。新求解/网格双库回归见 [solve-workflows.md](solve-workflows.md)。

验收结果：`tmp/mainline-N3-mesh-evidence/mesh-handler-result.json`为SQLite/PostgreSQL各23项；几何工作流各15项、发布输入各19项复验，core14/14、CAE最终3/3、执行兼容4/4、旧门禁最终5/5通过。开发库仍13表/旧head，临时PG已删除。源码归档`platform/backups/mainline-N3-mesh-20261102.zip`，当前源码匹配报告在同一证据目录。

日期：2026-11-02。N3-03 将 `mesh.generate@1` 接入统一处理器与工作流：预检 → 几何 → 显式 B 确认 → blockMesh/checkMesh → 显式 C 确认。网格证据同时服务于工作流确认、原阶段推进接口和后续发布输入引用。求解/后处理/报告的注册步骤仍待继续接入。

## 输入和实际执行

创建 `/api/v1/cases/{id}/workflow-runs` 时设置 `target: "mesh"` 和算例的 `run_dir`，其余 revision、幂等键、预算和归属语义与 [registered-workflows.md](registered-workflows.md) 一致。`caseDict.mesh.source` 必须是 blockMesh；`run_dir/system` 必须包含 blockMeshDict 与 controlDict。

`MeshHandler.prepare` 在 case 锁内选择 caseDict、system、constant 中除 polyMesh 外的内容，以及存在的 `0`、`0.orig`。初始场保持原样，旧网格、旧日志和旧质量报告不进入运行目录。当前有效几何 publication 的 validation 作为显式发布输入封存，记录生产版本、清单哈希和 current 策略。缺少注册几何成果时拒绝网格派发，不能借一个任意手工场景当作上游。

同一run中若已有geometry生产步骤，网格必须消费该步骤被确认的publication；确认后换了几何head会拒绝派发，不会自动借新结果。由B阶段显式新建的工作流可在预检后采用当前几何版本。

worker 使用当前支持的 WSL/OpenFOAM 环境，固定执行 blockMesh 与 `checkMesh -constant -allGeometry -allTopology`。准备记录固定 run_dir、WSL 发行版、OpenFOAM bashrc、worker 路径、本次执行 UUID 和预算；领取时验证固定命令，worker 复核实际发行版。两个工具的输出写入独立运行目录和 Job 日志，合计受日志预算限制。

实际工具版本、可执行文件路径与 SHA-256、worker SHA-256、Python 版本和 execution_id 写入工具链报告。worker 运行前后复核封存输入；工具改写输入、未生成网格、命令非零退出或质量证据不合格都会失败。API 发布还会重新计算质量、边界及文件哈希，不接受仅有退出码 0 的任务。

这里封存的是选定 case 文件。OpenFOAM 全部系统库、includeEtc 和其他外部依赖尚未做完整可迁移快照，属于 N2 工具链/外部依赖后续范围；此执行器也不构成任意原生代码的文件系统沙箱。

## 质量政策

必须同时具有真实 Mesh OK、完整生成网格和可解析的非正交度、skewness、aspect ratio。`qualityTargets` 中额外要求的指标也必须出现。指标和阈值必须有限；缺少指标、坏日志、Mesh OK 缺失、文件不一致不能通过任何放行政策。

| policy | 对已经测得的超限指标 |
|---|---|
| fail | 拒绝发布和 C 确认 |
| warn | 可发布，但保留 `quality.status: fail` 及所有违规条目 |
| allowWithReason | 仅在 caseDict 中有非空 reason 时允许；保留失败指标和理由 |

例如配置 minJacobian，而当前 checkMesh 输出没有该指标，即使 policy 为 warn 仍失败。质量政策不会合成缺失的数值，也不会把测量超限改成 pass。

## 产物与当前版本

每次成功发布具有独立不可变目录和数据库代次，产物包括：

- `run_dir/constant/polyMesh/` 的真实网格，至少含 points、faces、owner、neighbour、boundary。
- `run_dir/blockMesh.log` 与 `run_dir/checkMesh.log`。
- `B-mesh/quality.json`：指标/政策/输入与日志哈希/本次执行标识。
- `B-mesh/boundaries.json`：从实际 boundary 文件读取的 patch 名、类型、nFaces、startFace。
- `B-mesh/toolchain.json`：实际运行工具的来源记录。

Job/attempt、发布 head、状态事件和工作流步骤在同一终态事务更新；工作副本兼容文件在提交后修复。较早任务迟到只保留 superseded 历史计算，不覆盖新 head，也不能进入 C 确认。取消或失败不更换已有有效 head。

失败回退也检查发布代次：已有更新的成功发布时，较早任务的失败仍记录事件，但不回退当前工程阶段。最终23项双库验收包含“新网格已明确确认到C，旧任务随后失败”；旧无发布作业的阶段回退另经fallback-final复验。

几何 → 网格的发布依赖在启动、收尾、最终提交及 current 引用时复核。当前引用沿已登记的上游引用递归验证，拒绝循环及超过 16 层的依赖。几何输入变化或几何 head 更换，会使现有网格 current 引用失效；显式历史消费保持原版本，不擅自切换成新网格。完整失效图展示和自动重算仍未实现。

## API 与确认

| 接口（统一前缀 `/api/v1`） | 行为 |
|---|---|
| GET `/cases/{id}/mesh/results` | 当前不可变 quality、boundaries、toolchain 与 publication 元数据 |
| GET `/cases/{id}/mesh/results?publication_id=...` | 显式历史版本；检查同 case、mesh 类别、成功 Job 与产物哈希 |
| GET `/cases/{id}/mesh/publications` | 最近 50 个网格发布记录、状态、代次和当前标识 |

结果中 `stale` 表示当前引用策略是否仍成立；历史结果可读不代表可以据此推进。发布文件损坏返回 409，未知/其他 case/非网格版本返回 404。没有当前发布时返回 404，仍可查询历史列表。

原 `mesh:evaluate` 保留兼容用途。旧阶段门禁也重新从日志计算质量结论、要求完整polyMesh清单，并禁止warn/理由放行缺失指标。存在注册网格 head 后，手工重写质量报告或受管理网格文件会使 head 失效；重新评估一个日志不会冒充新的专业网格发布。

B 确认仍通过 caseDict:advance，并携带 workflow_step_id/expected_step_version。确认事务提交后，接口派发刚释放的已注册步骤。若准备失败，确认不会被谎报为回滚：响应包含 `workflow_dispatch.status: pending` 和原因，步骤仍为 ready；修复后可调用已有 `:dispatch`。这不是自动恢复或自动重放已领取任务。

C 确认绑定本 run 的网格 publication，复核其当前状态、上游几何和源输入，不会借其他 run 的新网格。原工程门禁也采用注册发布验证，并检查工作副本网格/日志/报告与发布记录一致；直接在磁盘改动这些文件也不能用旧报告推进。确认成功前，工程阶段仍为 B。

## 验收与剩余边界

`mesh_handler_smoke.py --postgres` 使用临时 API、SQLite/随机命名 PostgreSQL、真实 CadQuery 和真实 WSL/OpenFOAM。注入仅存在于测试临时模块，没有生产测试开关。覆盖实际链路、初始场保存、边界/工具链、政策、缺失指标、派发失败恢复、空成功/坏日志/改网格拒绝、生产版本确认、上游失效、历史消费、输入变化、迟到与取消。

最终结果及原始来源在 `tmp/mainline-N3-mesh-evidence/`，只在实际通过后写入主线台账。既有几何工作流、发布引用、调度/领取/收尾和核心/CAE 基准另行复验。

尚未完成：其他网格器、全部外部工具链封存、完整 WSL/Windows/MPI 进程树取消、过期所有者恢复、S3 发布事务、孤立目录/兼容文件修复、网格工作流前端及后续求解/后处理/报告处理器。本轮取消检查证明持久取消和不发布，不证明整个进程树都已终止。没有新数据库 schema；未迁移或重载开发实例。

计划仍按当前工程阶段取后续步骤；已到C再提交target=mesh只预检已有证据。当前重算可显式回A后重新执行到C，尚无直接mesh:generate或自动失效重算入口；该能力随N2-04/N5恢复与重试继续完善。
