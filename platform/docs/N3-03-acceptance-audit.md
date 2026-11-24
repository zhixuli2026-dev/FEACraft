# N3-03 网格主线原条件验收

最终结论（2026-11-06）：原验收条件通过，已关闭。本轮修订后的 SQLite/PostgreSQL 各 23 项通过，进程退出码 0、临时 PostgreSQL 数据库已删除。下面保留首次失败与修复过程；规范证据为 `tmp/mainline-N3-mesh-solve-closure-evidence/validation.json`。

2026-11-06。原任务为“接入 blockMesh/checkMesh 和质量政策，封存网格与边界清单”，原条件为“真正 Mesh OK、质量阈值和 policy 均检查；缺指标、改网格或坏日志不放行”。前置 N3-02 已按原条件完成。

最终结论：修订后的 SQLite/PostgreSQL 各23项全部通过，进程退出码0，临时数据库已清理，N3-03已按原条件关闭。证据为 `tmp/mainline-N3-mesh-closure-evidence/validation.json`。

首次失败记录：首次复验在 SQLite 的迟到网格场景失败，尚未关闭。旧测试在原生进程暂停期间等待新版本发布，与当前“同算例写入进程全部排空后校验”的约束形成循环等待；新网格已在 02:53:29 进入收尾，直到旧进程 02:55:21 超时才发布。实际 blockMesh/checkMesh 都已执行成功，失败不是 Redis 故障。已调整测试：新任务进入收尾即释放旧原生进程；迟到失败则在真实停止与排空记录写入后暂停终态提交，再确认新版本，检查旧失败不能回退状态。生产排空规则与超时预算不变，修订后的双库完整验收现已通过。

## 实现核对

- `mesh_worker.py` 在本次封存运行目录执行真实 blockMesh 和 checkMesh；检查工具退出码、Mesh OK 与实际指标，不复用已有 polyMesh/checkMesh 输出。gzip 规范化只处理本次独立目录内新生成的核心网格文件。
- `mesh_quality.py` 检查非正交、偏斜、长宽比及配置的其他指标；缺失指标、非有限值和无效阈值始终拒绝。fail、warn、allowWithReason 仅决定如何处理已测得的阈值违反，不能豁免缺失证据；allowWithReason 要求非空理由。
- `mesh_publication.py` 从实际日志重新计算质量，与输入清单、真实边界文件、工具链和本次 execution ID 对照；points/faces/owner/neighbour/boundary 及日志、质量、边界清单缺失或摘要变化时不可发布。
- B 确认绑定本流程几何生产版本；网格只消费已确认来源。C 确认重新检查当前网格、上游来源和活动文件完整性，不能借用另一个流程的新成果。失败或迟到结果不能覆盖当前有效发布。

## 本轮验收覆盖

`mesh_handler_smoke.py --postgres` 的 SQLite/PostgreSQL 隔离测试使用真实 CadQuery、blockMesh 和 checkMesh，覆盖：

1. 明确 B/C 确认、当前结果读取、原生网格/边界/工具链封存、0.orig 原件保留。
2. fail、warn、allowWithReason 有/无理由四种策略，以及 warn 下配置指标缺失仍拒绝。
3. 修改活动网格/质量/日志、损坏不可变原件、空执行、坏日志、执行后改网格均不能通过。
4. 确认后准备失败、旧生产版本失效、另一个流程的新结果、历史显式引用和权限隔离。
5. 实际网格执行被暂停期间改变输入、产生新发布、取消或迟到失败，当前发布和确认状态保持正确。

规范证据目录为 `tmp/mainline-N3-mesh-closure-evidence/`，以最终 result.json 及进程终态为准。本批不声称重新执行 S3 全网格矩阵；原 S3 工程链记录继续保留在 `s3-core-workflow.md` 和 `tmp/mainline-N2-s3-core-workflow-evidence/`。

## 范围承接

本项覆盖原计划的 blockMesh/checkMesh 注册主线。其他网格器、所有质量修复/交互界面仍按 295 项全功能计划交付；全进程树、资源上限、跨主机与完整恢复分别由 N4/N5/N7 承接。本项不以这些后续任务先完成作为关闭条件，也不将它们删除或宣布完成。
