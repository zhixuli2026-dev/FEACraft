# N4-06 公平队列与批次额度对账

## 201 项积压验收

2026-11-23，PostgreSQL 与真实 Redis 一次封存 201 项。积压账户在两个算例各提交两批 50 项，另一账户提交 1 项。两个实际 worker 启动前 Redis 有两个可运行消息。另一账户先完成，积压账户的首项当时仍在运行。第二 API 取消另一算例的 100 个未领取作业，所有作业均无 claim。核验 10 次跨账户读取拒绝、实际取消与失败后额度复用、两个 worker 退出及实际 DeepSeek 工具读回 2 次 HTTP 200，模拟 0。 [201 项回执](../../tmp/live-fair-queue-njh3l0z2/live-result.json) · [隔离库与 Redis 清理](../../tmp/fair-queue-broker-chh8351w/result.json)

SQLite 首轮在并发提交时收到 `storage_quota_busy`。使用同一幂等键复跑封存 201 项，最后一批提交后在客户端 180 秒期限内未收到响应。批次提交后改由同一算例的首项触发 Redis 唤醒，既有持久恢复扫描推进其余符合条件的作业。 [结算繁忙](../../tmp/live-fair-queue-8e66xk_j/live-result.json) · [已提交后超时](../../tmp/live-fair-queue-_icnhxs3/live-result.json)

改动后 SQLite 加真实 Redis 完成相同的 201 项验收，5 个提交全部收到 202。另一账户首先完成，100 个排队作业由第二 API 取消且均无领取记录。10 次越权读取被拒绝，实际取消、失败与成功后都复用了额度，两个 worker 正常退出。真实 DeepSeek 工具读回 2 次 HTTP 200。两种数据库合计 4 次真实 HTTP 200，模拟 0。 [SQLite 201 项回执](../../tmp/live-fair-queue-k0n6g3_9/live-result.json) · [隔离 Redis 退出证明](../../tmp/fair-queue-broker-58ynddr9/result.json)

## Redis 队首复验

旧排序直接使用算例上次领取时间。一个算例早先完成的 CAD 预览可能让它的新批次成员越过已有队首，实际 Redis 一度出现两个已发布的队首。现在用候选创建时间和算例上次领取时间的较晚值排序，再按作业 ID 确定顺序。两种后端均经过 API 中断、Redis 重启及实际双 worker 核对，50+50+1 租户积压也已复验。[原始失败](../../tmp/live-cli-startup-cy70jv_c/redis-head-failure.json) · [文件存储](../../tmp/live-cli-startup-yu_b5_dz/workflow-batch-redis.json) · [S3](../../tmp/live-cli-startup-n0f29rdb/workflow-batch-redis.json) · [积压复验](../../tmp/live-fair-queue-ezp405r7/live-result.json)

## V23 并发提交与租户数据隔离

2026-11-18，在隔离 PostgreSQL 库、Redis 和两个实际 worker 上，以每个用户一个执行名额复验。用户 A 先提交 50 项，再从另一个算例提交 50 项，同时用户 B 提交 1 项。启动 worker 前，101 项均未领取，Redis 只有两个可运行消息。调度候选前三项覆盖两个账户的三个算例。B 的作业完成时，A 的首项仍在运行。另一个 API 取消了 A 的 50 个排队项，均未取得 claim。额度随后可用于实际退出码 7 的作业及成功作业。[调度、进程与 HTTP 证据](../../tmp/live-fair-queue-43oiiz0p/live-result.json)

两名用户互查对方的算例作业列表、批次、单项详情、日志和文件，10 次读取均返回 404。修正了作业详情和日志的错误正文：缺失作业与无权读取的作业原本返回不同的说明；现在两种请求的公开状态和正文相同。两名用户分别验证这两个入口，独立 API 也拒绝跨用户取消。[验收入口](../server/live_llm_fair_queue_acceptance.py) · [作业路由](../server/app/routers/jobs.py)

以上两轮完整积压验收各有 2 次真实 DeepSeek 请求，最终一轮同时核对了缺失作业和无权访问作业的响应。两轮均等待各自的两个 worker 退出。[首轮](../../tmp/live-fair-queue-_7jkp644/live-result.json) · [最终复验](../../tmp/live-fair-queue-43oiiz0p/live-result.json)

2026-11-08。N4-06 的公平队列、批次取消和额度释放验收已通过：SQLite/PostgreSQL 各 101 项积压及双真实 worker，另有实际本地取消/收尾等待测试。3 个主要验收均由真实 DeepSeek 工具回读收尾，共 6 次 HTTP 200、25,712 tokens，模拟 LLM 为 0。N4-05 的其余资源预算任务继续保留。

## 已落地的实现

- 恢复扫描按算例内、计费用户内两层排序，交替选择用户与算例；保留 SQL 投递意图和原 attempt，取代单纯按 Job ID 截取前 64 项。
- Redis 优先投递每个用户的可运行算例队首，其他请求保留在 SQL。批次并发已满时继续检查该用户其他算例；实际 claim 仍重新核验并发额度和原输入绑定，投递前检查不占用执行额度。
- 批次提交采用部署选定的 local/Redis 模式；Redis 批次进入实际 worker，不在 API 内另起本地执行器。
- 额度暂不可用返回 deferred，保留原排队请求，避免将正常等待当作 worker 业务失败。
- 批次取消在同一 case 锁和 SQL 事务中记录所有成员的取消，然后唤醒实际 owner/worker。运行中的原生范围关闭仍由原 owner 核验；并发额度沿用实际写者/停止证明判定。

实现：`job_delivery.py`、`batch_jobs.py`、`routers/jobs.py`。显式真实模型入口：`live_llm_fair_queue_acceptance.py --source-profile-id 26`，默认 50+50+1 项；小规模诊断不替代完整积压验收。

## 验收与保留失败

- `tmp/live-fair-queue-scpxc_oz`：首轮 101 项尚未进入 Redis，0 模型调用；队列为空，验收失败并保留。
- `tmp/live-fair-queue-c0wt13yk`：小规模实际诊断确认错误引用了旧 Job 模型不存在的 started_at 字段；0 模型调用，失败保留。已改为 JobAttempt 的实际开始时间。
- `tmp/live-fair-queue-ox7i9h65`：小规模真实 Redis/双 worker、跨用户推进、独立 API 取消、实际失败后额度重用及真实 DeepSeek 回读通过。该次只有 1+1+1 项，不作为 101 项或完整 N4-06 完成证据。
- `tmp/live-fair-queue-dbwb_y5k`：SQLite 完整通过。首轮 101 项中，100 项属于同一用户的两个算例；另一个用户和第二算例出现在扫描前三位 `[1,101,51]`，Redis 只有 2 个消息。两个真实 worker 运行，另一用户先完成，积压用户仍运行；实际开始/完成/取消时间亦可在隔离库核对。独立 API 拒绝另一用户取消、原用户取消 50 项未领取工作后没有启动标记，重复取消无新增操作。
- `tmp/live-fair-queue-e3vp95m1`：PostgreSQL 同样通过 5 组功能验收，并直接核验另一用户完成时积压作业仍处于 running。两套测试均核验：取消后的唯一额度可重用，实际退出码 7 的失败不泄漏额度，随后成功；用户额度 2、单批额度 1 时，另一算例可完成，原批第二项仍 queued。两套实际 worker 均正常退出，两个隔离 Redis 范围均核验停止，临时 PostgreSQL 数据库已删除。
- `tmp/live-cli-startup-jvsifptg`：本地补充测试在旧算例尚缺持久停止证明时被正确拒绝；0 模型调用，保留失败记录，未放宽算例保护。
- `tmp/live-cli-startup-wjitj63l/local-queue-wait.json`：改用同用户另一安全算例，实际旧进程已核验停止但暂缓提交收尾；新作业被准入并保持 queued、没有 claim。释放收尾后，新作业实际执行恰好一次、成功并核验停止。接口已将正常容量等待保留为排队结果，避免准入已提交后再返回启动冲突。

三组主要验收的真实模型证据分别在相应目录的 `live-result.json` 和 `upstream.jsonl`。两个 Redis 验收各有 2 次模型 HTTP 请求，分别 8,527、8,625 tokens；本地补充验收 2 次、8,560 tokens。这些是实际平台 Agent 工具调用，不冒充 CLI 推理。小规模诊断中的模型用量未混入上述合计。

| 原验收条件 | 当前证据 |
|---|---|
| 用户洪泛不饿死其他用户 | 两库各 100+1 项；前三位包含两个用户和两个积压算例；双真实 worker 下另一用户先完成 |
| 取消排队项不启动 | 独立 API 取消 50 项，claim 均为空且未产生进程标记；所有成员取消在单个 SQL 提交内生效，再发唤醒 |
| 失败不泄漏并发额度 | 用户额度 1 下取消后继续执行实际失败/成功；额度 2 与批次额度 1 分别核验；本地收尾未提交前维持占用，提交后只启动一次 |

完整资源硬限制、远端主机关停/接管、旧计划迁移、MPI/UDF 执行模式桥接，以及全量部署/故障矩阵仍归 N4-05、N4-07、N3-07、N5/N7 原任务。本阶段不删除这些交付要求。

## 统计与后续

N4-06 本阶段收口后，主线 **34/53 完成、剩 19 项**；全功能 295 项仍待整项验收。后续汇报同时给出可使用的功能、实际验收结果与主线计数。

此前“主线约 1–3 周、全功能约 4–8 周”为低置信度的初步排期估算，未建立稳定完成速率，不能视为可承诺交期。剩余范围包括执行/资源 4 项、恢复策略 4 项、API/界面 6 项、回归/部署 6 项。295 项历史基线包含 159 项标为完成、118 项部分实现、14 项暂缓、2 项实验、2 项未确认；这些历史标签仍须按当前要求核验。
