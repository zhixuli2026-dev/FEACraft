# 托管 MPI 求解的输入封存

2026-11-22，确认 C 后的托管 MPI 求解把原生算例检查、确认网格归档和 MPI 生成配置放在写事务外准备。工作流入队复核原版本和上游网格，确认生成配置和封存成员一致后，提交作业、成果预留和投递。运行仍使用原有 `decomposePar`、`mpirun` 与 `reconstructPar`。

旧版在本地 SQLite 复制真实 40 MiB 时阻塞独立写入，同算例保存为 409。新版复制期间允许独立写入，同账户另案保存返回 200，只有一个求解作业。双进程 `icoFoam` 到达 0.5 s，生成重建速度场并完成确认 D。生成配置、原生回执、发布摘要使用相同执行 ID。 [旧版对照](../../tmp/n4-workflow-mpi-baseline-2uzzouxw/workflow-mpi-baseline.json) · [SQLite 复验](../../tmp/n4-workflow-mpi-fixed-exvzllbv/workflow-mpi-fixed.json)

首轮验证程序从公开摘要读取进程数，实际字段位于独立原生回执。保留该失败回执，复验使用原生回执与封存配置核对两个进程。模型调用为 0。 [首轮验证记录](../../tmp/n4-workflow-mpi-fixed-jqqvj4_s/workflow-mpi-fixed.json)

认证 SeaweedFS 配 SQLite 的 40 MiB 复验允许独立数据库写入和同账户另案保存，HTTP 返回 200。真实双进程、重建速度场、生成配置和确认 D 均通过。服务退出和 S3 监听关闭已复核。 [SeaweedFS 复验](../../tmp/n4-workflow-mpi-s3-fixed-dldb_hnb/workflow-mpi-fixed.json) · [完整验收](../../tmp/N4-05-workflow-mpi-seal-20261122/validation.json)
