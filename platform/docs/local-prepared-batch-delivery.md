# 本地准备批次读取原始执行参数

N3-07 原任务包含现有本地批次入口和 Redis 路径。Redis worker 的成功不能替代本地 `BatchState` 验收。本单元保持53项主线与295项全功能目标，整项关闭须另按原条件审计。

## 已复现的问题与修复

旧 `batch_jobs._start_when_capacity` 根据 `Job.command`、批次目录和请求默认 runtime 调用 `start_job`，遗漏专业处理器持久化的 argv、超时、日志预算和执行证据目录。普通命令的默认值可能恰好相同，专业处理器则被原 SQL claim 以 `invalid_request` 拒绝；独立投递扫描器抢先正确领取时会掩盖此缺陷。

反例 `tmp/live-cli-startup-j96m2p6z/local-prepared-legacy-counterexample.json` 使用原源码包 `mainline-N3-07-private-cache-cleanup-20261109.zip` 中相同的生产模块，运行实际几何、网格和 UDF 构建后提交三个专业成员。MPI/UDF 的 argv、超时、日志预算不一致，串行的 runtime、超时、日志预算不一致。三者均在领取前被真实执行器拒绝；无编造的进程或模型结果。该负向对照按预期失败，真实模型0，临时 PostgreSQL 已删除。

生产修复读取最新 admitted attempt 的原始 `execution_spec`，通过原 `intent` 校验持久化摘要，核对 case、请求者、本地投递模式和整批成员/并发策略，再逐项传递原 command、case_prefix、runtime、argv、timeout_seconds、max_log_bytes、evidence_run_dir。原 claim 的封存、SQL 并发、归属和取消校验继续生效，不重新构造默认值。

## 验收方法与当前证据

`live_llm_local_prepared_batch_acceptance.py` 在隔离 API 的 startup 中设置真实 `delivery_stop` 并等待原扫描任务结束，保持原工作流派发可用；不替换投递结果。`start_job` 的观察器记录参数并原样调用原函数。MPI ranks、UDF 实际映射库、串行 icoFoam、退出身份、SQL 行和场文件均取实际结果。最后的真实 DeepSeek 回合仅独立读取工程证据，不宣称模型自主规划这些工程操作。

固定验收范围为三个模式的400单元0.5时刻结果、同一用户/批次并发、每个模式真实运行中取消和其余成员从未领取、旧场与取消前成果指针、实际 API 退出、新 API 同键重试及明确新三模式批次。S3 恢复 API 使用独立空缓存。文件存储与 S3 的最终结果以本单元 validation 为准，进行中不记通过。

独立旧 commands 兼容入口 `local_batch_command_acceptance.py` 已通过，证据 `tmp/local-batch-commands-3o6egl6h/acceptance.json`：两个实际 Python 进程、真实 stdout、用户上限1/批次上限2、实际最大并发1、持久化参数与实际启动完全相同、同键仍为原作业、API 已退出。此检查属于实际 OS/SQL，不调用或模拟 LLM。

## 失败与原始证据

- `tmp/live-cli-startup-ek8roq0v`：首轮负向脚本错误等待所有 Job 变为 failed；注册处理器不接受通用外部失败写入，实际保留 queued，之后显式取消清理。按真实启动错误修正观察条件后，第二轮得到上述负向对照。第一轮保留失败，模型0、清理无错、临时库删除。
- `tmp/live-cli-startup-5xdi7u_m`：三个真实求解及三种运行中取消均通过，实际 API 退出后整个公开 DTO 的相等断言失败；未保存该轮前后字段差异，不能反推精确原因。模型0、清理无错、临时库删除。
- `tmp/live-cli-startup-h2meajr5`：补存全量历史后，列表比较因 SQL `IN` 查询返回顺序变化而失败。原前后记录按 Job ID 对齐后全部字段相等，诊断保存于 `local-restart-keyed-history-diagnosis.json`；公开 DTO 仅运行成员的 phase 从 completed 变为 cancelled。夹具现按原 manifest 成员顺序返回记录，不修改实际 SQL 行或运行结果。该轮仍保留失败，模型0、清理无错、临时库删除。
- 公开 Job `phase` 在原 API 中优先展示本地进程生命周期，在新 API 中来自持久化 attempt。后续修正验收同时保存两份原始 DTO 和全部 Job/attempt/submission 行，必须保持 SQL 历史不变；只允许实际观察到的本地 `completed` 到持久化 `cancelled` 视图差异，其他字段差异仍失败。该既有显示差异另由 N6-06 刷新一致性任务处理，不当作持久历史变化，也不静默忽略。

审查记录包含入口、实际导入的辅助代码及哈希，见 `tmp/local-prepared-batch-before-fix-live-review-v2-20261109.json`、`tmp/local-prepared-batch-fixed-live-review-v2-20261109.json` 和 `tmp/local-batch-command-review-20261109.json`。未运行历史模拟 LLM 回归集合。

## 本轮最终结果

2026-11-09（本地准备批次）：旧源码实际复现MPI/UDF/串行三成员参数丢失、claim前invalid_request，生产BatchState改读并校验原持久spec。FS/PG及S3/PG独立投递扫描器真实停止，本地三模式400单元结果、各模式运行取消/其余不领取、原生出生身份退出、旧成果、实际API退出/幂等/明确新批次通过；完整SQL历史不变，公开本地completed到cancelled的视图差异实测单列。旧commands两个实际Python进程及用户1/批次2兼容通过，属实际OS/SQL。新增4次真实DeepSeek、17136tokens，仅独立证据回读，模拟0；三轮验收失败和旧码负向对照保留。主线36/53、剩17；N3-07进入原条件与兼容最终审计，原四条remaining保持。

FS：tmp/live-cli-startup-orti50c1；S3：tmp/live-cli-startup-jdurs6a4。实际原生退出身份共40个；实测公开phase投影差异6项，SQL完整历史对照3批/后端均相同。

源码、审查、退出及原条件对照见 tmp/mainline-N3-07-local-prepared-batch-evidence/validation.json。
