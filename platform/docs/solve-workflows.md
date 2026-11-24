# 注册串行求解、场验证与 D 确认

2026-11-06 N3-03网格与N3-04串行求解按原条件完成：真实网格双库各23项、求解双库各25项，本地文件计划7项、S3条件写入14项及旧后端拒绝1项通过。修复求解旧输出清理回归，并调整与排空约束冲突的迟到结果测试；未放宽生产校验。主线29/53完成，剩24（6进行中/18待开始）。下一步N3-05后处理、N3-06报告。全功能295项与N4-N7继续，正式SIWC联调仍暂缓。

验收结果：SQLite/PostgreSQL各25项；网格各23项、几何工作流各15项、发布输入各19项复验，core14/14、CAE3/3、日志/计划/调度兼容3/3、最终门禁5/5。源码归档 `platform/backups/mainline-N3-solve-20261102.zip`，当前源码匹配记录见同名证据目录。开发库仍13表/旧head，临时PG为0。

日期：2026-11-02。`solve.openfoam@1` 将串行求解接入已有持久工作流：几何 → B 确认 → 网格 → C 确认 → 求解 → D 确认。原通用 Job 求解入口继续兼容；新的注册工作流使用固定处理器、独立尝试目录和不可变求解发布。

## 输入和执行

`POST /api/v1/cases/{case_id}/workflow-runs` 使用 `target: "solve"`，其余字段、预算、幂等键与 [registered-workflows.md](registered-workflows.md) 相同。已有阶段决定从哪个节点开始；到达 D 后再提交同目标只预检，不自动重算。需要重新执行整条流程时显式回 A，再创建新运行。

同一运行的 solve 步骤必须消费该运行已确认的 mesh publication。确认 C 后如果准备失败，已提交的确认保留，响应中 `workflow_dispatch.status` 为 pending；显式 dispatch 可重试准备，但不能换用另一条运行产生的新网格。由 C 阶段新建的求解工作流可以使用当前有效网格。

封存输入包括 caseDict、system、constant 中的非网格输入、0 和存在的 0.orig；网格从 mesh publication 复制完整 polyMesh 文件。工作副本中的旧时刻、旧日志和旧求解报告不进入新尝试。网格及其上游几何须成功、当前、文件哈希和源指纹有效；执行前、完成时和最终发布事务内继续复核依赖。

`controlDict.application` 必须是安装在当前 OpenFOAM `FOAM_APPBIN` 下的原生 `*Foam` 求解器，并与 caseDict 中已指定的 numericalMethod.solver 一致。固定 WSL worker 使用所选运行目录，调用单个串行求解器。当前契约要求显式非空 0 目录、startFrom=startTime、startTime=0、stopAt=endTime、有限且正的 endTime/deltaT；不能把 latestTime/restart 当作新的独立计算。不会把 0.orig 隐式覆盖到 0。尚未支持解析 include/macros 来推导控制值。

## 成功证据

`solve_artifacts.py`、`solve_worker.py` 和 `solve_publication.py` 分别承担纯数据验证、实际执行和发布校验。

- 初始输入及 polyMesh 在执行前后保持相同；每个输入记录必须与封存清单对应。
- 日志须正常收尾、无 fatal、含实际求解残差和时间；时间不能倒退，残差不能为负或非有限值。带相名称的字段名保留点号，不截断为前缀。
- 末时刻必须存在对应场文件，并符合请求 endTime；允许原生步长/输出精度的舍入。提前结束仅在 fvSolution 配置 residualControl 且日志明确报告收敛时进入独立的 `residual_control_converged` 分支；本轮真实基准覆盖的是到达 endTime 的路径，不据此宣称所有稳态求解器已验收。
- 每个输出时刻通过 VTK OpenFOAM reader 实际读取体场及其边界值；检查时间值、求解字段存在、字段名/类型、分量、网格单元数、数组长度和有限数值。当前真实基准覆盖标量和向量；张量类型虽有分量检查，仍需独立实算验收。重算哈希清单也不能绕过坏场检查；surface/粒子等其他输出按原文件哈希封存，尚未宣称逐值数值验证。
- `solve-evidence.json` 与重新计算出的输入、日志、输出清单完全相同；`solve-toolchain.json` 包含本次 execution_id、worker/原生执行辅助代码 SHA-256、实际求解器路径/哈希、OpenFOAM/WSL/Python 版本。

工具链来源检查用于区分本次执行和旧产物，不是任意 OpenFOAM 字典或用户代码的安全沙箱。完整工具链离线封存、额外求解器和物理模型仍在全功能范围内。

## 发布和工作副本

发布包含本次所有时刻的输出文件、solve.log、solve-evidence.json、solve-toolchain.json 与 `.foam` 标记。不可变目录和持久 head 使用已有 artifact publication 协议：失败、取消和源变更不能替换当前结果；旧执行迟到可以成为 superseded 历史，但不能借新结果完成自己的 D 确认。

head 提交后修复工作副本。对同一 run_dir 以前已提交的结果，新结果不再包含的旧路径只有在现有字节与旧发布哈希匹配时才删除。用户修改过的文件、未知历史文件和损坏历史清单所对应的文件均保留；这些残留会阻止 D 确认。不会递归删除目录，也不会清理另一运行目录。旧不可变版本始终保留。

工作流状态提交可能早于兼容副本修复完成；Job API 的本地成功状态等待完整收尾。修复失败产生 `artifact.alias_repair_required`，不谎报已提交发布被回滚。该行为不能替代 N5 的完整文件系统/数据库恢复日志。

## 查询与工程确认

| 方法与路径（`/api/v1` 前缀） | 行为 |
|---|---|
| GET `/cases/{case_id}/solve/results` | 读取当前不可变 evidence/toolchain 和 publication 元数据 |
| GET `/cases/{case_id}/solve/results?publication_id=...` | 读取明确历史版本，返回 current/stale；历史失去当前资格仍可查看原始证据 |
| GET `/cases/{case_id}/solve/publications` | 最近 50 个求解发布的 ID、代次、状态、attempt 和当前标记 |

越权、其他 case 或其他产物类别返回 404；发布清单/文件损坏返回 409。stale 检查包括当前 head、求解输入和递归上游来源；它不表示可把历史版本直接用于新的工程确认。

D 确认仍调用 caseDict:advance，必须明确 confirmed，携带当前 revision、workflow_step_id 和 expected_step_version。它绑定本运行的求解生产版本，复核当前依赖、工作副本中的初始输入/网格/日志/报告/场文件哈希，以及完整输出时刻集合。缺场、额外旧场、网格变化或错误 postProcessing.runDir 都不能通过。存在 solve head 后不能用旧通用 Job 证据绕过；从未建立 solve head 的旧算例继续走既有求解门禁。

## 验证与剩余范围

`server/solve_handler_smoke.py --postgres` 在独立 API、SQLite 和临时 PostgreSQL 中执行真实 CadQuery、blockMesh/checkMesh、icoFoam 与 laplacianFoam。故障注入只位于测试临时模块，生产没有测试开关。二维/三维腔流、热传导解析解、二进制和压缩 ASCII 分开验证；OpenFOAM v2406 对 binary 自动禁用压缩。热传导注册测试采用 20×4×4 网格通过全部 checkMesh 检查；原通用基准保持其既有网格与验收范围。

当前取消验证覆盖持久终态和不发布取消结果，挂起测试 worker 由夹具显式释放；不代表完整 WSL 原生进程树终止。并行/MPI、restart/resume、自动依赖重算、全部物理模型、完整崩溃恢复和工作流 UI 仍保留在原计划中。后续 N3-05 已接通注册后处理版本消费与 E 确认，见 [postprocess-workflows.md](postprocess-workflows.md)；未建立 solve head 的旧 case 保留工作副本兼容入口。

本轮没有数据库 schema 变化，未迁移开发库或重载开发 API。验收结果、任务状态和源码归档校验以 `tmp/mainline-N3-solve-evidence/`、[mainline-task-status.json](mainline-task-status.json) 和 [full-feature-tasks.json](full-feature-tasks.json) 的最终记录为准。
