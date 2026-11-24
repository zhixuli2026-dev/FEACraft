# 托管串行求解的输入封存

2026-11-22，确认 C 后的托管串行求解先准备算例文件和已确认的网格成果，再进入写事务。入队复核写权限、算例修订号、原工作流及步骤版本、执行预算、网格成果绑定和源文件。认证对象存储的成果描述用于确认求解目录，随后通过成果输入校验完整核验网格内容。

同一实际 40 MiB 算例在旧版文件系统配 SQLite 上阻塞独立写入，同算例保存返回 409。新版在复制期间允许独立写入，保存返回 200，只创建一个求解作业。真实 `laplacianFoam` 使用已确认网格，生成 `1/T` 并完成确认 D。此后处理整链以同样的新版派发完成确认 E，提取结果为 350 K。 [旧版对照](../../tmp/n4-workflow-solve-baseline-9mlvakwm/workflow-solve-baseline.json) · [新版串行求解](../../tmp/n4-workflow-solve-fs-fixed-u_djovey/workflow-solve-check.json) · [下游回归](../../tmp/n4-workflow-postprocess-fs-fixed-6997cbu_/workflow-postprocess-check.json)

认证 SeaweedFS 配 PostgreSQL 在实际复制期间允许同账户另案保存，返回 200，求解继续完成确认 D。文件系统配 PostgreSQL 在复制中取消返回 200，实际 I/O 排空后没有求解作业。 [SeaweedFS 复验](../../tmp/n4-workflow-solve-s3-fixed-2k901hgn/workflow-solve-check.json) · [PostgreSQL 取消回执](../../tmp/n4-workflow-solve-fs-cancel-chxsdf2y/workflow-solve-check.json)

托管 MPI 求解仍能复现确认 C 后的长写事务。用实际双进程 `icoFoam` 算例复制 40 MiB 时同算例保存为 409，放行后产生重建速度场与 MPI 回执。 [双进程对照](../../tmp/n4-workflow-mpi-baseline-2uzzouxw/workflow-mpi-baseline.json)

本批使用真实 OpenFOAM、VTK 和存储。模型调用为 0。串行求解验收纳入 N4-05 的 RB-12 与 RB-14。 [完整验收](../../tmp/N4-05-workflow-solve-seal-20261122/validation.json)
