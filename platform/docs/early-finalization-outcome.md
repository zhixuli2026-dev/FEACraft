# 早期收尾失败与中断状态

2026-11-10更新：下文为原批次历史记录，历史主线计数不代表当前进度。现为37/53、剩16项；两处原LaTeX缺失停止证明已增加绑定快照的明确人工保留处置，并在原真实故障库副本及临时PostgreSQL验证。停止证明仍不补造，原门禁保持。见[人工处置及N5-04原条件对照](finalizing-manual-disposition.md)。

本批修复上一批真实 LaTeX 回归发现的两个状态卡点，保留原测试断言：停止回执提交持续失败时显示失败；收尾 API 退出且尚无持久化发布意图时显示中断。两者都保留原作业、尝试、私有结果及原始子进程记录，不自动重跑，不伪造进程清理证明。

## 原因与实现

1. `finalizer_receipt_fail`：原生编译器已结束，但停止记录的数据库提交被故障注入持续拒绝。产物验证已返回错误，代码仍按成功发布去等待所有写入者排空，而缺少停止记录的当前子执行正是无法通过排空的对象。现在已被拒绝的产物跳过成功发布的来源预留，最终事务核验绑定并提交失败。没有把它标记成排空成功；原停止记录仍缺失，公开详情保留 `process_cleanup_unverified` 恢复提示。
2. `finalizer_owner_death`：原工作进程的出生身份已确认退出，但作业仍停留 `finalizing`。现在仅对已注册处理器、有效独立输入绑定、完整登记身份、尚未形成发布/复制意图且发布预留未提交的记录，原子记录 `interrupted`、结束时间、失败的发布预留和恢复提示。活动发布指针不变，所有私有文件保留。未知历史、外机身份、仍活跃的原执行者和已有持久化发布意图继续走原有核验/恢复路径。

`failed`/`interrupted` 表示作业结果已经结束，不等于已经取得所有进程的可信停止证明。因此没有停止证明的历史记录仍不能为新的来源校验提供排空许可。恢复提示明确下一步是核验进程清理；后续恢复操作与缺失证据处理仍属于 N5-04/N5-07，不能把本批状态修复当作完整恢复交付。

## 验证

- `early_finalization_smoke.py --postgres`：SQLite 和 PostgreSQL 各执行原套件两个真实 LaTeX 故障场景，包含原生进程/后代退出、无关进程存活、用户输入不变、新 API 重读。追加数据库断言验证结束时间、失败发布预留、活动指针未推进、排空检查点仍为空、恢复提示和未知子执行记录未被伪造。结果：`tmp/early-finalization-evidence-j19x8g7v/result.json`。
- `publication_finalizing_smoke.py --modes live_owner corrupt_bundle two_peers`：活动原执行者不被中断，损坏的已准备发布仍需核验，双 API 恢复完整发布仅提交一次。SQLite 三场景通过，结果：`tmp/finalizing-evidence-tp1h43r5/result.json`。
- `job_reconciliation_notice_smoke.py`：原身份/世系/并发探测/权限等 SQLite 17 场景通过，结果：`tmp/reconciliation-notice-evidence-0q0x0oiv/result.json`。
- `calculix_publication_smoke.py`：本轮共享收尾改动后的 SQLite 14 场景通过，结果：`tmp/calculix-publication-evidence-k1lc2zv1/result.json`。

没有把上批 13 个通过场景与本批两个定向场景拼成“本轮原生监督全套通过”。原始失败日志仍在 CalculiX 验收档案；本批修改的是产品代码，未放宽原故障断言。开发数据库和端口 8000 未改动。

## 剩余范围

主线保持 20 完成、14 进行中、19 待开始，剩余 33/53；295 功能清单不改整项完成标记。本批解决可见状态卡住，尚未补齐缺失物理回执的所有恢复方式、未注册旧任务和 S3 收尾。其余专业入口的独立输入/发布迁移继续推进，下一批为 SU2 与 ParaView。
