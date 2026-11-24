# 执行成果的存储准入争用

并行的 S3 批次中，一个成员已完成真实网格，但成果归档遇到账户结算锁后直接失败；另一轮出现存储 429，持久资源事件记为 `storage_limit/archive`。原生日志均有 `MESH_WORKER_OK`，不是网格生成失败。失败现场保留在 `tmp/live-cli-startup-6alu3z3c` 和 `tmp/live-cli-startup-42bnjcs5`，两轮真实模型调用均为 0，不能登记成模型验收通过。

第一次现场的本地日志明确记录 `storage_quota_busy`。第二次没有保存当时的账户预留行，不能声称逐行还原了其 429 的来源；实际账户预留专项证明了同一类争用：未结算写入会暂时预留几乎全部可用额度，其他归档此时不能把这份临时占用解释成永久额度不足。

## 修复

`quota.check_storage_quota` 在持有账户准入锁时区分真实额度不足和其他尚未结算的账户写入预留。预留保持全额计费，临时争用返回 `storage_quota_busy`，已结算或不确定远端请求占用的额度不被擅自释放。

`execution_storage_admission.admit` 用于输出归档、不可变日志和成果复制的准入阶段。每次申请使用独立事务；已回滚、释放锁后才等待，随后重新检查归属、执行身份和额度。只重试明确的账户结算忙，沿用当前尝试的原截止时间；无执行上下文时最多等待 30 秒。真实 429、复制进程占用和其他冲突继续拒绝。它不重跑原生工程，也不重发结果不确定的远端写入。

## 验收范围

显式入口 `live_llm_workflow_batch_professional_acceptance.py --source-profile-id 26 --postgres`，S3 加 `--s3`。账户锁、账户预留、原时限和真实额度耗尽属于实际基础设施/预算故障检查。MPI/UDF 由真实 Redis worker 执行，模型在工程完成后独立回读证据；不宣称模型自主规划了专业工作流。

工程场景包含两个独立 case 的固定工作流成员：实际两子域分解/求解/重构，实际 wmake/CDLL/UDF 求解，400 单元速度场比较、固定库和网格来源、原 API 退出、新 API 接续、明确 D/E 确认和真实后处理。专业模式运行中取消、worker 猝死、同 case 多 C-run 来源策略以及原 S3 UDF 单次 409 仍需继续。

运行目录和归档校验见下节。主线保持 36/53、剩 17，N3-07 未关闭，295 项全功能范围不变。

## 当前通过证据

2026-11-09（MPI/UDF混合批次）：当前修复版FS/PG与S3/PG均完成9个真实Redis worker原生作业、两子域MPI、wmake/CDLL/UDF、400单元速度场及实际后处理，终止原API后不重放求解并明确D/E接续。修复成果归档/日志/复制遇到账户结算争用的直接失败，真实锁/账户预留/原时限/真实额度不足专项通过。含修复前FS共6次真实DeepSeek、25972tokens、模拟0；两轮S3失败和证据不足边界保留。主线36/53、剩17，N3-07继续同case来源及取消/恢复，原409未关闭。见[修复与证据](execution-storage-admission.md)。

当前FS：tmp/live-cli-startup-5h9_ph07；当前S3：tmp/live-cli-startup-0kqeuj17；两份professional-entry-source.py与当前入口逐字节一致。先行FS的ttffdoe2为修复前历史补充，不能代替修复版验收。通过场景共27个真实原生作业、6个已核验退出的worker；含失败场景共10个worker出生身份退出核验、5次Redis停止、5个临时PG删除、3个自建S3停止。模型调用仅为工程证据回读。

真实账户预留专项保留约2GiB临时额度，先观察结算忙，再由原持有人释放，准入随后成功；没有增加套餐额度。持续锁争用在原0.2秒预算到期后拒绝；真实零额度仅申请一次并返回429，随后恢复原套餐。最后两项属于预算/协议检查，不冒充真实工程回合。

源码包platform/backups/mainline-N3-07-typed-batch-professional-20261109.zip；SHA、全部Python AST和逐文件摘要核验见tmp/mainline-N3-07-typed-batch-professional-evidence/validation.json。历史包含模拟模型或写开发数据库的集合未执行，未绕过回归审查登记。
