# 托管网格的输入封存

2026-11-22，确认 B 后的托管网格派发在写事务外读取几何成果并准备网格输入。入队仍检查写权限、算例修订号、原工作流和步骤版本、执行预算、几何成果绑定及源文件。网格输出保留原 OpenFOAM 质量检查，确认 C 继续引用本次网格发布。

旧版文件系统配 SQLite 在实际复制 40 MiB 输入时阻塞独立写入，同算例保存返回 409。新版在相同位置允许独立写入，同算例保存返回 200，随后完成唯一一次真实 `blockMesh`，生成 `constant/polyMesh/owner` 并通过确认 C。 [旧版本](../../tmp/n4-workflow-mesh-baseline-9_vtrkgl/workflow-mesh-baseline.json) · [SQLite 复验](../../tmp/n4-workflow-mesh-fs-fixed-i21w_282/workflow-mesh-check.json)

文件系统配 PostgreSQL 在实际复制期间响应取消。返回后物理 I/O 和暂存均已排空，未创建网格作业。既有后处理整链也用新版网格派发重新验收，实际提取 350 K 并完成确认 E。 [取消回执](../../tmp/n4-workflow-mesh-fs-cancel-8pno11d4/workflow-mesh-check.json) · [后处理整链回执](../../tmp/n4-workflow-postprocess-fs-fixed-khwf99wy/workflow-postprocess-check.json)

认证 SeaweedFS 配 PostgreSQL 在实际复制期间允许同账户另案保存，返回 200，网格继续完成确认 C。SQLite 在封存后遇到等长替换时返回 `input_stale`，没有入队。恢复输入后显式重新派发，实际网格成功。 [SeaweedFS 验收](../../tmp/n4-workflow-mesh-s3-fixed-d6h3m_k7/workflow-mesh-check.json) · [替换与重试](../../tmp/n4-workflow-mesh-fs-late-qhelzmcy/workflow-mesh-check.json)

确认 C 后的托管串行求解仍在写事务中复制输入。文件系统配 SQLite 的 40 MiB 对照阻塞独立写入，同算例保存返回 409。放行后 `laplacianFoam` 生成了实际时间步文件并完成确认 D。 [求解对照](../../tmp/n4-workflow-solve-baseline-9mlvakwm/workflow-solve-baseline.json)

本批使用实际 CadQuery、OpenFOAM 和存储。模型调用为 0。托管网格验收纳入 N4-05 的 RB-12 与 RB-14。 [完整验收](../../tmp/N4-05-workflow-mesh-seal-20261122/validation.json)
