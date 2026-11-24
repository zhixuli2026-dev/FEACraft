# 工作流后处理派发的输入封存

2026-11-22，已确认 D 的托管工作流先读取求解归档和复制后处理输入，再进入写事务。工作流入队仍复核调用者权限、算例修订号、工作流版本、步骤版本、原预算及已确认的求解成果。入队、成果预留和投递一并提交。输入准备期间收到工作流取消时，等待实际读写排空后释放暂存。

使用同一真实 OpenFOAM 导热算例对照。本地 SQLite 旧版在 40 MiB 复制期间阻塞独立写入，同算例保存返回 409。新版同处保存返回 200，只生成一个后处理作业。该作业绑定原求解成果并提取 350 K，随后完成确认 E，工作流成功。

SQLite 并发派发只绑定同一个步骤尝试。PostgreSQL 本地文件路径在复制中取消成功，实际 I/O 排空后没有后处理作业。SQLite 等长替换触发 `input_stale`，恢复原文件并显式派发后正常完成。独立后处理入口也完成 350 K 提取、原键重放和历史指标读取。 [专项证据](../../tmp/N4-05-workflow-postprocess-seal-20261122/validation.json)

SQLite 的超时对照单独暂停后台恢复，以区分请求本身和独立的恢复任务。超时请求排空物理复制后没有入队。确认 D 已提交，显式派发只创建一个作业，得到 350 K 并完成确认 E。启用后台恢复时，恢复器可以接手仍处于就绪状态的步骤。 [超时隔离回执](../../tmp/n4-workflow-postprocess-fs-deadline-veal_tcz/workflow-postprocess-check.json)

认证 SeaweedFS 的首轮确认暴露额度探针未及时结算，下一次封存因 `storage_quota_busy` 延后。明确结算后，真实 40 MiB 复制期间允许同账户另案保存，随后捕获第二个问题。后台恢复器和确认请求同时派发时，后到的请求在首个作业已入队后取得了新的算例存储租约，阻断了原作业的启动。现在租约获取在同一数据库锁内复核步骤是否就绪，已绑定的步骤复用原作业。认证 SeaweedFS 配 SQLite 在实际复制期间取消成功，没有留下后处理作业。 [启动失败证据](../../tmp/n4-workflow-postprocess-s3-fixed-h23b9__o/launch-errors.txt) · [取消回执](../../tmp/n4-workflow-postprocess-s3-cancel-aavxopnb/workflow-postprocess-check.json)

最终 PostgreSQL 正常自动派发与 SQLite 显式并发派发各只取得一个封存租约，只创建一个后处理作业。PostgreSQL 在 40 MiB 复制期间同账户另案保存返回 200。SQLite 的两次派发均返回 200。两套环境都从已确认的求解归档提取 350 K，并完成确认 E。 [PostgreSQL 自动派发](../../tmp/n4-workflow-postprocess-s3-fixed-s2ok2p80/workflow-postprocess-check.json) · [SQLite 并发派发](../../tmp/n4-workflow-postprocess-s3-duplicate-rvbhb5yg/workflow-postprocess-check.json)

托管网格仍有另一个可复现的长事务。确认 B 后复制真实 40 MiB 输入时，SQLite 拒绝独立写入，同算例保存返回 409。放行后，实际 OpenFOAM 网格作业成功并提交了 `constant/polyMesh/owner`。该路径仍纳入 RB-12 和 RB-14。 [网格对照](../../tmp/n4-workflow-mesh-baseline-9_vtrkgl/workflow-mesh-baseline.json)

计算验收使用实际 OpenFOAM、VTK 和存储。模型调用为 0。证据纳入 N4-05 的 RB-12 与 RB-14。
