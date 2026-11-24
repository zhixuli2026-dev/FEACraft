# N6-03原子恢复：实现与实际验收

2026-11-11。工作流恢复方案、恢复提交和同确认查询已接通，串行检查点和专业模式实际验收已完成。按[原API条件核对](N6-03-original-api-closure-audit.md)关闭N6-03，主线40/53、剩13；原N4、N6-04～07及N7和全部295目标继续。下节保留各阶段证据，不将其扩大为全产品交付。

## 行为和事务

`GET /workflow-runs/{id}/resume-plan`返回原步骤、尝试及版本、当前修订、原输入重试/检查点方式和阻碍。`POST /workflow-runs/{id}:resume`要求固定`Idempotency-Key`、原流程/步骤/尝试版本、当前修订、`confirmed:true`和明确`mode`；检查点方式另需`checkpoint_time`。`POST /recovery-request`接受`action:resume`按同账户、键及完整参数查回已提交回执。

服务端重建v5注册图，按真实工程阶段开始，旧流程保持终态。旧Job/attempt/步骤不重置；新attempt沿用原请求族并记录直接来源。新图、步骤、新Job、封存输入、发布预留、审计、投递意图在一次事务内提交，之后才调用正式执行器。新尝试的`step_id`从分配时即存在，创建/投递/恢复三个事件均有完整run/step/attempt关联，避免先提交独立Job再补步骤。普通重算继续使用当前输入正式处理器，不带入旧检查点。

读取和派发均检查恢复图与原来源、处理器、预算及新尝试归属。原来源全列摘要保持，允许请求族head在合法新尝试分配后推进。检查点准备在控制写锁外执行，提交前复核来源、工程快照和短会话记录的发布读集；原预算保持。UDF求解重试的已发布编译前缀需验证原引用，不默许重新编译。B/C/D/E仍由原工程确认接口推进，恢复不代替用户审批。

## 实际通过范围

显式入口`platform/server/workflow_resume_acceptance.py`及导入辅助代码均已审查；未运行历史模拟集合。最终v5审查和冻结源码分别为`tmp/N6-workflow-resume-review-v5-20261111.json`与`tmp/N6-workflow-resume-runtime-v5-20261111.zip`，476份源文件。

- SQLite：`tmp/workflow-resume-SQLite-luw34yln/resume-result.json`，4组通过。
- 临时PostgreSQL：`tmp/workflow-resume-PG-a162dmrw/resume-result.json`，4组通过。增量迁移、精确删除及独立目录查询确认库不存在，见`tmp/workflow-resume-PG-owner-hdb8sq6o/result.json`。
- 每库实际执行3个CadQuery Job：自动预览、注册流程原生运行取消、新流程按原封存重试并导出。6个Job均有有效停止证明，两个API保留的Windows句柄确认退出。
- 同一API上的两个并发HTTP请求保留同键，最终只有一个新Run/Job/attempt；仅对既有`storage_quota_busy`/`validation_busy`做有期限的同键重试，其他冲突仍失败。每库实际记录1次存储结算忙。
- 版本/修订/未确认/不支持检查点拒绝不分配新执行；完成后同键回读、异内容拒绝、退役head拒绝、父子导航、旧四类行全列保持、跨账户隐藏及只读协作者不可写通过。
- 实际发布后新流程等待B确认，未自动推进。另独立读取两库保留的真实STEP/STL，共4份；STEP头尾和非空STL三角形、字节大小与SHA已记录在`tmp/N6-workflow-retry-native-artifacts-20261111.json`。

本段真实模型0、模拟LLM0；这是实际原生/SQL/API验收，不是模型回合验收。纯参数拒绝与真实原生执行分别记录。

## 发现、修复及失败保留

真实取消暴露公共收尾缺陷：`phase(finalizing)`在durable Job取消后提前把attempt结束，而新归档入口要求它仍处于收尾阶段，导致`ValueError`和缺失停止/排空证明。现保持`cancelling`至原worker完成真实停止、归档和写入排空，再提交attempt终态；两个归档入口仅接受原worker的已取消收尾。未放宽停止、身份或额度校验。双库最终真实运行取消均完成原收尾。

首轮`tmp/workflow-resume-SQLite-u2t0rrzj/resume-result.json`因验收只等Job显示cancelled而过早读取停止字段失败；该轮缺失记录不修补，API及已记录进程出生身份退出另存`post-failure-birth-readback.json`。第二轮`tmp/workflow-resume-SQLite-kcd1qsex/resume-result.json`保留真实收尾缺陷，API及5个已记录出生身份退出另存同名回读文件，不把回读当原SQL停止证明。

第三轮`tmp/workflow-resume-SQLite-59zp5mu8/resume-result.json`已完成取消排空，暴露同键请求等待控制锁后未先查回执的竞争，已在初次控制锁之后重新查回执。第四轮`tmp/workflow-resume-SQLite-y4j6wjll/resume-result.json`保留验收未处理既有存储结算忙的失败；产品代码未因该轮修改。后两轮全部实际Job停止及API退出通过，不将其整体记为通过。

## 工作流级真实检查点与数值对照

显式入口`platform/server/workflow_solver_resume_acceptance.py`及全部导入辅助代码已审查。调用真实CadQuery、blockMesh/checkMesh、icoFoam、VTK；没有模型回复替代或CLI推理模拟。每组7个实际Job：自动几何预览、流程几何、网格、原求解取消、第一次续算取消、第二次续算完成、从0开始的新普通流程求解。四组合共28个Job、8次实际检查点续接；本段真实模型0、模拟LLM0。

| 组合 | 实际结果 | 审查版本 |
|---|---|---|
| FS＋SQLite | `tmp/workflow-checkpoint-FS-SQLite-n9yer__f/checkpoint-result.json` | v1 |
| FS＋PostgreSQL | `tmp/workflow-checkpoint-FS-PG-ftjd19_4/checkpoint-result.json` | v1 |
| S3＋PostgreSQL | `tmp/workflow-checkpoint-S3-PG-jlicqwgs/checkpoint-result.json` | v2 |
| S3＋SQLite | `tmp/workflow-checkpoint-S3-SQLite-9h2ljb0t/checkpoint-result.json` | v3 |

各组完成400单元实际算例，原求解写出0.005后取消，第一次从0.005继续、写出0.01后再取消；第三个同根attempt从0.01继续，日志首个执行时刻0.0105，最终0.5。原Run/step/Job/attempt全列保持、直接来源、同键回执、预算和当前正式发布均核对。无效/未写时刻被拒绝且不分配新Job。S3两组先把本机原结果目录移至隔离保留目录，再从完整远端归档构建独立续算视图；未重建旧运行目录，临时读取视图清理通过。

续算与普通fresh求解的U、p最终场四组最大绝对差均为0。另独立从实际8个400单元网格读回，核对U/p均有限且非零，并保存文件SHA和数值范围：`tmp/N6-workflow-checkpoint-numeric-readback-20261111.json`。S3＋SQLite另经真实阶段接口明确确认D，恢复Run实际进入succeeded；随后由测试账户显式A/B/C确认回到C，再运行原控制文件的普通求解，未自动审批。其他三组验证新流程等待D确认及明确取消。

所有28个Job均有有效停止证明，4个API保留Windows句柄确认退出。两个通过的临时PG分别见`tmp/workflow-resume-PG-owner-mlkmjdnz/result.json`、`tmp/workflow-resume-PG-owner-dzus0fjv/result.json`，均增量迁移且独立确认已删除。两个通过的自建S3 Windows/原生出生身份退出见`tmp/s3-attempt-store-anw3icyh/workflow-checkpoint-store-result.json`、`tmp/s3-attempt-store-ywv562d9/workflow-checkpoint-store-result.json`。S3客户端120秒等待仅是验收客户端配置，生产200秒原任务预算保持；请求耗时单独保留，不作为性能验收。

首次S3＋PG失败保留：`tmp/workflow-checkpoint-S3-PG-legr22t7/checkpoint-result.json`。原任务已真实停止，但归档准入只允许running/finalizing，拒绝cancelled/cancelling，实际日志为`Output archive ownership or captured inventory changed`，远端回执不完整，因此续算正确拒绝。修复仅允许精确原worker、原claim/head、未改变清单和完整根/子进程停止证明的取消收尾归档，仍按原额度和期限提交条件对象，不授予执行或成功发布权限。失败轮4个实际Job、API、自建S3亦已停止，临时PG已删除，见`tmp/s3-attempt-store-ww4k4xwz/workflow-checkpoint-store-result.json`及`tmp/workflow-resume-PG-owner-7s6nuose/result.json`。不将该轮记为通过。

v1/v2/v3冻结记录为`tmp/N6-workflow-checkpoint-review-v{1,2,3}-20261111.json`和对应`runtime-v{1,2,3}` ZIP。FS在v1通过，后续唯一生产修改为未经过FS分支的S3归档准入；v2通过S3＋PG，v3仅新增显式D确认的验收分支，生产源码保持。旧完整回归集合未执行。

## MPI/UDF实际原输入重试

UDF前缀投影只有id/kind/generation/status/manifest_sha256，原恢复分支错误索引attempt_id。现从ArtifactPublication及原build步骤/attempt/Job构建完整引用，逐层核对case、生产者、发布字段和真实复用输入。历史来源可保留已superseded的原发布，准入的新重试另查当前head和源内容；不把旧投影补成虚构执行记录。

最终显式入口`workflow_professional_resume_acceptance.py`v3的4组目标场景均通过：

| 模式/组合 | 实际证据 | Job数 |
|---|---|---:|
| UDF＋FS/SQLite | `tmp/workflow-udf-retry-FS-SQLite-befc8uhq/professional-result.json` | 6 |
| MPI＋FS/PG | `tmp/workflow-mpi-retry-FS-PG-t6lq05ge/professional-result.json` | 5 |
| UDF＋S3/PG | `tmp/workflow-udf-retry-S3-PG-tjsq0l4p/professional-result.json` | 6 |
| MPI＋S3/SQLite | `tmp/workflow-mpi-retry-S3-SQLite-f3n7ua_3/professional-result.json` | 5 |

在原native子进程树内按出生身份观察UDF的icoFoam或两个MPI rank，取消后独立观察这些进程已退出，再以原封存重试原步骤。新旧Run/step/Job/attempt保持关联，旧历史全列不变；新流程等待D并经显式确认成功。MPI最终1600单元，UDF400单元，U/p有限且非零；MPI/UDF请求checkpoint方式被明确拒绝，不暗中改成重试。

UDF只有一个实际编译Job，新恢复图的udf-build为已发布前缀复用、没有新attempt；只新增一个求解Job。实际wmake/CDLL、加载标记、完整发布和投影代次损坏拒绝通过；测试故障仅改SQL投影，原JSON与更新时间精确恢复。另从两组实际重试目录独立读回ELF，与原build输出及新封存引用逐字节SHA相同，见`tmp/N6-workflow-UDF-library-readback-20261111.json`。

审查/冻结源码为`tmp/N6-workflow-professional-review-v3-20261111.json`和`tmp/N6-workflow-professional-runtime-v3-20261111.zip`，480份源文件，全部导入辅助代码已审查，无推理替代。22个通过轮次Job、4个API退出通过；两临时PG删除见`tmp/workflow-resume-PG-owner-8npogiri/result.json`及`tmp/workflow-resume-PG-owner-bi8s3n5m/result.json`。S3 Windows/原生owner退出见`tmp/s3-attempt-store-5ghpyx4t/workflow-checkpoint-store-result.json`及`tmp/s3-attempt-store-4wap4ejq/workflow-checkpoint-store-result.json`。本段真实模型0、模拟LLM0。

v1的UDF首轮已有实际复用通过（`tmp/workflow-udf-retry-FS-SQLite-ksdu32q1/professional-result.json`），但取消观察在本机复制后，未当作当前的原生运行中观察证据。v1 MPI（`tmp/workflow-mpi-retry-FS-PG-v5u513rl/professional-result.json`）在100输出时刻的reconstructPar退出-9，原因未证明；其4个Job及API退出、临时PG删除保留，不计通过。后续MPI使用1600单元、10输出时刻并直接观察真实两个rank；不能据该成功反推首轮已修复。资源/性能诊断继续原N4-05/N7-04。v2两轮（`tmp/workflow-udf-retry-FS-SQLite-yuf2jyfg`、`tmp/workflow-mpi-retry-FS-PG-xds1u00a`）因观察脚本未处理identity=None失败，原生/API均已停止；v3仅修观察空值，生产不变。

## 后续原范围

API原条件核对见[N6-03退出记录](N6-03-original-api-closure-audit.md)。原N6-04～07仍需JobsPanel、CasePanel、完整版本/历史源码阅读、选中状态及刷新恢复和完整浏览器流程；完整故障、跨API/主机矩阵与资源/性能故障仍由原N4/N7继续，53/295目标没有缩减。
