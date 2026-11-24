# N7-01 同算例的单 worker 失联与尝试隔离

2026-11-19。N2-03 的原条件是同算例不同工况目录并发时，各尝试的输入、运行目录及结果相互隔离，初始场和用户原件保留。V04 已有真实 PostgreSQL/S3 双工况正常并行，另有同时中断两名专业 worker 的恢复证据。本轮补一个执行所有者实际退出、另一个仍在运行的组合。[N2-03 原条件](mainline-acceptance-review.md) · [V04 正常并行](same-case-serial-batches.md) · [MPI/UDF 双 worker 猝死](professional-worker-death-recovery.md)

## 显式输入 `convert` 的原始失败

首轮两个 Windows 作业各自在真实 SeaweedFS 封存的私有工作目录运行。左侧提交显式 `input_paths` 的 typed `convert`。所属 worker 实际退出后，第二 API 观察到原尝试中断，真实 Redis 重投返回 `already_owned_or_terminal`。右侧所属 worker 没有退出，但右侧进入发布前来源校验时收到 `validation_job_busy`。该轮以未通过保留，临时 PostgreSQL、SeaweedFS 和 Redis 已回收。[原轮次回执](../../tmp/attempt-single-owner-loss-yy4y_cdd/result.json) · [原入口](../../tmp/attempt-single-owner-loss-yy4y_cdd/attempted-entry.py) · [幸存 worker 日志](../../tmp/attempt-owner-api-8kwpgquz/batch-worker-1/worker.log)

显式选择走独立输入封存，默认 `convert` 走注册处理器。首轮显式选择在执行前缺少守护记录，事后记录的原生根退出仍不足以证明整个 Windows Job 范围已排空。算例级来源校验继续拦截缺少原始证据的历史尝试。[提交与封存](../server/app/services/job_execution.py#L184) · [算例写者校验](../server/app/services/job_write_checkpoint.py#L141) · [人工核查](../server/app/services/job_recovery_disposition.py)

## 显式输入 `convert` 修复与复验

- Windows `convert` 直接使用显式封存路径时，复用现有原生 Job 守护登记。适用条件包括原始命令与作业绑定相符、有效选定输入封存，以及独立命令执行。该路径沿用既有的输入选择、私有执行目录与发布方式。恢复仍要求原始守护准备、退出观察和同主机范围核验相符。守护登记缺失的历史尝试继续保留写者限制。[守护登记](../server/app/services/job_scope_guardians.py#L49) · [排空证书](../server/app/services/job_scope_recovery.py#L37)
- 真实 PostgreSQL 先后记录了左侧启动意图 20、原始守护准备 21 和原生启动准备 24。`C-run/left` 只封存自身 `0/U` 与 `0.orig/U`。左 worker 以 37 实际退出后，独立 API 读取原尝试的 `interrupted`，原守护的退出观察为 35。v4 排空证书逐字段匹配 PostgreSQL 的原准备与观察记录，原 `process_stop` 仍为空。右 worker 实际消费 Redis 重投并返回 `already_owned_or_terminal`。[真实启动与恢复回执](../../tmp/attempt-single-owner-loss-o1pnarlk/result.json)
- 右侧注册 `command.convert` 在左侧恢复期间仍持有同一原生进程，放行后用原尝试完成发布。公开 API、SeaweedFS 实时路径和不可变版本的结果字节相同。左侧部分结果只存在私有目录，两侧实时及封存的初始场和原件均保留。全程两作业、两尝试。两个 API、两个 worker、Redis 和 SeaweedFS 已核验退出，临时 PostgreSQL 数据库已核验删除。[包含对象存储的运行回执](../../tmp/attempt-single-owner-loss-o1pnarlk/result.json) · [独立回收核验](../../tmp/attempt-single-owner-loss-o1pnarlk/cleanup-independent.json)

FF-A16 和 FF-A24 合入后再用当前源码完整复跑。左 worker 以 37 退出，核对原始守护记录后得到排空凭证。Redis 拒绝重投，右侧原作业通过 SeaweedFS 的发布和字节校验。独立确认数据库、对象存储、队列和进程均已清理。[当前源码复验](../../tmp/attempt-single-owner-loss-jxyjqn5o/result.json) · [独立回收核验](../../tmp/attempt-single-owner-loss-jxyjqn5o/cleanup-independent.json)

## 封存 shell 的独立对照

- 两个实际 Windows 原生作业分别从不同 worker 的私有目录读取各自封存的输入。左侧封存 shell 写入私有结果，并改写私有 `0/U`。测试只终止该 worker，实际退出码 37，独立探测确认左侧原生根退出。第二 API 读取原 attempt 的 `interrupted`。恢复器随后持久化独立的 v4 `recovered_stopped` 排空证书，原缺失的 `process_stop` 仍为空。[原生出生身份与排空凭证](../../tmp/attempt-single-owner-loss-1uq1ir14/result.json)
- 上述对账期间，右侧 worker 和原生根均经独立探测确认仍在运行。放行后，右侧注册 `command.convert` 从同一个 attempt 完成，持久停止回执确认范围清空。公开读取、生产 S3 目录和不可变版本的右侧结果逐字节一致。左侧部分文件未公开，两侧实时和封存的 `0/U`、`0.orig/U` 八份字节一致。[作业与实际 S3 回执](../../tmp/attempt-single-owner-loss-1uq1ir14/result.json)
- 右侧 worker 实际消费左侧的 Redis 重投，返回 `already_owned_or_terminal`。PostgreSQL 共两个作业、两次原尝试。两个 API 和两侧已知原生根均确认退出，右 worker 正常退出。Redis 的实际停止回执核验通过，另从 `pg_database` 及 WSL `/proc` 核验临时库已删除、SeaweedFS 已退出。[真实重投](../../tmp/attempt-owner-api-l4qgcplh/batch-worker-1/deliveries.jsonl) · [独立回收核验](../../tmp/attempt-single-owner-loss-1uq1ir14/cleanup-independent.json)

## 复验入口

入口通过现有 `no_llm` 审查登记后运行。验收使用 D 盘隔离目录、真实 PostgreSQL、SeaweedFS 和 Redis/arq，两个 API 共享临时数据库和对象存储。默认左侧为显式选定输入的 Windows `convert`，右侧为注册的 `command.convert`。添加 `--sealed-shell-left` 可复验左侧封存 shell 的对照。所有运行的真实和模拟模型调用数均为 0。[显式入口](../server/attempt_single_owner_loss_acceptance.py) · [回归审查规则](../tools/regression_llm_policy.py)

```powershell
& 'platform/server/.venv/Scripts/python.exe' -B -X utf8 `
  'platform/server/attempt_single_owner_loss_acceptance.py'
```

两个 worker 均运行在当前 Windows 物理机上，使用独立进程、私有缓存和真实归属。实际跨物理主机的网络隔离、宿主掉电及接管继续按 N7-01 原范围核对。[执行主机证据边界](remote-worker-routing.md)
