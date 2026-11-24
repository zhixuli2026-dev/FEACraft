# 托管 UDF 编译和集成的输入封存

2026-11-22，确认 C 后的托管 UDF 编译和集成分别准备原生输入。实际复制及生成配置在写事务外进行。集成阶段同时固定已确认的网格和该工作流的编译成果，入队时复核两条上游、工作流版本、算例修订号、写权限和预算。

旧版两条路径使用同一个 `icoFoam` 空腔算例，各自在复制 40 MiB 时阻塞 SQLite 独立写入。同算例保存均返回 409。放行后实际 `wmake` 编译和动态加载成功，求解生成真实速度场并完成确认 D。 [编译旧版对照](../../tmp/n4-workflow-udf-baseline-fs-build-njl97ajp/workflow-udf-check.json) · [集成旧版对照](../../tmp/n4-workflow-udf-baseline-fs-integrate-sna5zkky/workflow-udf-check.json)

新版文件系统配 SQLite 在编译输入复制期间允许独立写入和同账户另案保存。认证 SeaweedFS 配 SQLite 在集成输入复制期间实测同样结果，HTTP 均返回 200。两组各生成一次独立编译成果和求解成果。求解装载原库并绑定已确认网格，封存配置与原生回执的执行 ID 一致，随后完成确认 D。 [本地编译](../../tmp/n4-workflow-udf-fixed-fs-build-o4cyvf29/workflow-udf-check.json) · [SeaweedFS 集成](../../tmp/n4-workflow-udf-fixed-s3-integrate-jn9b0s7n/workflow-udf-check.json)

本批使用真实 CadQuery、`wmake`、OpenFOAM 和存储。模型调用为 0。UDF 相关验收纳入 N4-05 的 RB-12 与 RB-14。 [完整验收](../../tmp/N4-05-workflow-udf-seal-20261122/validation.json)
