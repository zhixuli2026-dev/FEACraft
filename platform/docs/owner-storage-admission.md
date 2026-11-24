# RB-12 部分验收：S3 创建、克隆与恢复的持久用户额度

## 2026-11-19 账户预留对账轮转

旧对账固定选择最早创建的 100 条记录，长期保留的记录会占满候选页。新对账分别选择尚未取得关闭回执的预留和已有回执但仍保留字节的预留，两类各最多 16 条。按上次核验时间和 ID 排序。读取损坏或无法核验的记录时，保留原字节和回执，记录本次观察时间，让后续候选能进入下一页。独立保存点避免单条记录的异常影响同轮已核验的结果。已有请求期限仍约束开始处理下一条记录。[账户预留实现](../server/app/services/owner_storage_budget.py)

如果还有活动、身份不明或未关闭的写入，新申请仍返回 `storage_quota_busy`。账户锁下已经核验的对账结果会在拒绝前提交。原写入进程仍活动时，预留继续保留全部字节。进程退出后的关闭仍要求原进程身份与实际退出证据。结果不明的可变对象、损坏的关闭记录及摘要不匹配的对象继续占用额度。有效完整的不可变对象使用原长度和 SHA-256 校验结算。

SQLite、PostgreSQL 分别与实际启用版本的 SeaweedFS 运行专项。每类测试页缩至 2 条以验证轮转，测试在隔离数据库注入 6 条历史形态记录并写入实际对象。第一次维护返回 409 后，已有远端关闭回执的预留结清，尚未轮到的不可变对象保持原额。第二次仍返回 409，后来进入候选页的不可变对象结清。隔离 API 实际退出并重启后，原活动预留核验退出、写入新关闭版本并结清，新文件正常保存。未知三笔合计 65,627 字节始终保留，损坏的记录保持原 S3 版本和字节。[SQLite 证据](../../tmp/live-cli-startup-1nq1m86h/reservation-rotation.json) · [PostgreSQL 证据](../../tmp/live-cli-startup-t2_jz0ap/reservation-rotation.json) · [复跑入口](../server/live_llm_owner_reservation_rotation_acceptance.py)

两种数据库的分块上传中断与取消回归也已通过。每组实际退出 API 两次，五个迟到写入各得到 412，分块预留结清，原终止回执按精确字节核验。版本化专项与上传回归共 8 次真实 `deepseek-flash` HTTP 200，模型通过工具回读验收摘要。此前两次未显式启用版本的专项另有 4 次 HTTP 200，阶段共 12 次，模拟调用 0。[SQLite 上传回归](../../tmp/live-cli-startup-j296_fxi/upload-cleanup.json) · [PostgreSQL 上传回归](../../tmp/live-cli-startup-9mqj7_kn/upload-cleanup.json) · [早期 SQLite 回执](../../tmp/live-cli-startup-ccz4c3ba/live-result.json) · [早期 PostgreSQL 回执](../../tmp/live-cli-startup-dxk7hkd7/live-result.json)

本阶段使用现有时间戳与回执格式，无数据库迁移。恢复上一版代码仍能使用现有回执。独立算例写入预留的轮转、旧记录压缩及其他存储维护仍由 RB-15 跟踪。[资源工作清单](resource-budget-worklist.md)

后续更新：8个纯文件编辑API的S3额度已在[文件编辑增量](file-owner-storage-admission.md)接入；下文保留当时范围，当前剩余入口以资源清单为准。

2026-11-06：账户级持久预留已接入 S3 算例创建、ZIP 导入、算例复制、工作区复制、备份恢复，以及创建记录恢复和维护。它补齐未提交算例尚无持久 ID 时的远端写入额度边界。普通文件事务和其他长文件入口仍需审查，RB-12 尚未整项完成。主线 33/53 完成、剩 20 项；资源预算阶段 11/17 完成、剩 6 块。

## 业务事务前预留

`OwnerStorageReservation` 只依赖已经存在的账户，不依赖尚未提交的算例。入口在开始业务写入前使用独立事务保存本次可用额度，绑定账户、实际存储配置、操作 UUID 和写入进程出生身份。SQLite 如果已经开始真实业务事务则拒绝接入，避免第二个写事务死锁；不提前提交调用者事务。

预留额度目前等于账户实际剩余空间。操作内的正文、逻辑 HEAD、before/after 副本、创建计划、恢复历史及额度记录本身共同消耗这笔上限。每次物理 PUT 都检查条件、长度和摘要，SDK 单次请求，正文流受准入长度限制。操作自己的预检只在同一 SQL 会话、同一额度上下文中排除自身预留；其他请求仍计入完整活动预留。同一账户的活动创建操作未结算时，新操作返回 `storage_quota_busy`，不会被误报为永久额度不足。

这种预留会暂时占用全部可用额度，也可能在写入已落地但尚未结算时保守地同时计入实际字节和预留。它保证分配上限，并不保证多文件操作一定完成；元数据和保留版本同样需要空间。它也没有承诺同一账户的并行创建吞吐量。

## 远端写入记录与关闭

每个操作在账户私有 `.storage-capacity/<UUID>/` 下保留条件能力探测和 `head.json`。探测对象永久保留，在符合条件语义的服务端最多形成两个 8 字节版本；重试和恢复不通过删除探测对象重新获得条件。

发送业务 PUT 前，先用条件写入更新额度记录，其中包含最后一笔可能在途的请求、累计分配和新随机版本标识。记录持久化得到确认后才发送正文。正文和额度记录的累计字节不能超过 SQL 中预留的上限，并始终留出最终关闭记录的空间。单条记录最多 16 KiB；关闭及能力探测的固定余量是 16 KiB 加 16 字节。不会为未记录的正文发送请求。

正常结束、异常和取消都会先等待本地写入线程停止，再条件写入永久关闭记录。新版本阻止迟到的旧额度记录继续推进。关闭成功后，已确认请求由物理对象与保留版本计费；结果不明的正文请求继续预留自己的全部长度。额度记录本身响应丢失时，后续关闭会阻止其迟到写入；在没有关闭证明前，不释放整笔额度。

进程退出后的恢复先核验进程出生身份，再关闭旧记录，只保留最后一笔可能未完成请求的额度。已经存在永久关闭记录时，无须等待原进程退出，可以按其证明结算。完整不可变对象还能通过长度与 SHA-256 核验结算未知请求；可变 HEAD、缺失或损坏对象不会仅凭进程退出就释放额度。

## 提交、回滚和维护

正常入口保留原有提交时机。调用者选择暂不提交时，结算变更写入其原事务并只 flush；若随后回滚，最初的持久预留仍存在，永久关闭记录允许后续结算。故障恢复不会重放原生任务，也不会为了结算而修改算例的业务提交证明。

维护任务的创建恢复及历史清理使用这一额度上下文；提交该阶段后退出上下文。后续[逐算例维护](case-maintenance-storage-admission.md)先只读筛选，再在算例写锁之前取得独立账户额度，恢复已有上传和执行尝试记录；不新发放上传或执行额度。遇到活动创建者，维护会等待事务锁或返回明确的繁忙响应，调用方可在原操作结束后重试。

已提交且没有发送任何远端请求的空操作直接结算 SQL 预留，不额外写关闭对象。历史清理只在核验通过、确实准备删除过期记录时才验证条件能力，避免这两个入口的无工作调用产生远端版本。已结清的 SQL 预留回执支持分批压缩和原文恢复。[回执压缩验收](reservation-history-compaction.md)

本阶段曾有一次扩大到整个维护路由的检查失败：逐算例 `case_write.coordinate` 的普通文件恢复即使没有待恢复事务也会刷新 S3 HEAD。失败记录保留在本阶段证据中。后续[逐算例维护增量](case-maintenance-storage-admission.md)通过只读候选筛选解决没有候选工作时的增长，并为真实恢复增加账户额度；真实恢复的写锁与 HEAD 隔离仍保留。无法清理的候选记录仍可能触发恢复写入，普通文件 API 的隐含恢复也尚待准入审查。

## 验收与剩余范围

隔离 SQLite/PostgreSQL、真实启用版本的 S3 检查正常写入、并发竞争、正文及记录响应丢失、关闭响应丢失、进程退出、业务回滚、调用者延后提交、额度耗尽、预留阶段取消和写入阶段取消。另验证四类构造入口在文件写入中断及提交响应丢失后的恢复，以及自动/手动维护、本地额度和既有 staging 预留兼容。

迁移 `d1b6d82957fe` 新增账户预留表和约束；有任何历史记录时拒绝降级。开发数据库与开发服务未改动。具体场景计数、源码绑定和迁移证据见[阶段验收](../../tmp/mainline-owner-storage-evidence/validation.json)和[源码绑定](../../tmp/mainline-owner-storage-evidence/source-binding.json)。

剩余限制继续跟踪：

- RB-12：普通文件编辑、文件事务、ZIP/备份及其他长文件操作的完整节点/用户峰值入口审查；此阶段没有把所有存储写入自动视为已覆盖。
- RB-14：准入、恢复盘点仍在账户事务锁内执行 I/O，原创建业务事务也仍持锁写文件；线程等待与期限需要继续治理。
- RB-15：当前恢复按创建时间最多取前 100 个活动预留，长期未知记录可能阻挡后面的记录；损坏记录、非本节点进程身份、未知可变对象、记录及版本的维护仍需完整协议。永久额度关闭记录和条件探测不能由普通历史清理删除。
- RB-16/17：全局并发治理、跨节点执行以及应用工作目录之外的操作系统资源限制仍未完成。

## 2026-11-20 旧预留回收和终端续租

普通写入准入、人工文件恢复和人工上传恢复现在共用 `recover_owner`。先读取限量原登记、远端关闭日志、原生进程身份及对象证明；只有原进程确认退出时才允许 CAS 关闭未关闭的远端日志。随后在短事务核对账户版本、原额度登记和上传状态，结算仍无法证明的记录会保留。过期的账户版本让整次证明失效，只在原期限内重读。候选仍按更新顺序分页，未知请求仍按活动额度计算。

- 基线和修复都在认证 SeaweedFS 已写入 143,360 B 正文、未写入进度的位置让隔离 API 以 79 退出。原版暂停实际正文摘要核验时，独立 SQLite 写事务不可开；修复后另一个账户的算例修改返回 200，同时一个独立写事务将原账户版本从 6 推进到 7，旧证明被拒后重读。原上传两条转移分别结算 143,360 B 和 0 B，正文实际只 PUT 一次；最终读回摘要一致。[基线](../../tmp/storage-owner-proof-8p2ww1h7/storage-admission.json) · [复验](../../tmp/storage-owner-proof-bxu4jk8f/storage-admission.json)
- 把真实证明读取暂停到 8 秒期限之后。接口等待 I/O 排空后返回 503，该笔活动转移和正文额度在过期前后均保留。另一账户可改算例；其实际非特权 WSL PTY 命令已执行，SQLite 中终端心跳与租约均继续推进约 10 秒，随后可正常关闭并核验停止。原分块重试完成，仍只有一次实际正文写入。[过期、心跳和重试](../../tmp/storage-owner-deadline-ynox1j1u/storage-admission.json)
- 隔离 PostgreSQL 与认证 SeaweedFS 的取消复验两次真实 API 退出通过，5 个迟到条件请求返回 412、额度归零；真实 DeepSeek `deepseek-flash` 两次 HTTP 200，工具回读准确，模拟 0。[取消](../../tmp/live-cli-startup-v9kg77ax/upload-cleanup.json) · [模型](../../tmp/live-cli-startup-v9kg77ax/live-result.json)
- PostgreSQL + SeaweedFS 人工文件恢复七项和四个实际 API 退出见证通过；SQLite + SeaweedFS 人工上传恢复三项和两个实际 API 退出见证通过，含旧上传续传、错误内容拒绝、取消崩溃后同标识续接。两项均为实际基础设施验证，无模型调用。上传脚本首轮引用已删除的旧函数而无法启动，复跑把退出点接到当前的原生取消结束、SQL 结算前。[人工文件](../../tmp/manual-file-S3-hmdm9nrz/s3-result.json) · [人工上传](../../tmp/upload-recovery-S3-w9sqa1by/upload-result.json) · [首轮脚本错误](../../tmp/upload-recovery-S3-xdigv_ay/upload-result.json)

- 移除旧锁内回收入口后，认证 SeaweedFS 下的 SQLite 与 PostgreSQL 重新通过分块额度及退出故障矩阵各 12 项，未知请求保留额度，已存在的旧分块只复用。每库 6 个有效分块各只发生一次正文写入。[SQLite](../../tmp/upload-chunk-credit-da_1bvgs/checks.json) · [PostgreSQL](../../tmp/upload-chunk-credit-_9aiklzc/checks.json)

可在服务端虚拟环境执行 `python -B storage_admission_inventory_acceptance.py --owner-recovery` 与 `python -B storage_admission_inventory_acceptance.py --owner-recovery-timeout` 复跑，脚本在启动前核对只含实际基础设施的回归登记。

无新增迁移、开发库或现有 Docker 服务变更。锁外证明仍使用有界的只读会话来核对必要的 SQL 绑定；当前活跃写入的尾部结算与整体高负载、取消故障组合仍按 RB-14 验收。

## 2026-11-20 主动取消和已登记额度结算

取消可以直接中断盘点、远端读取或等待。正在进行的 I/O 仍需排空。只有已经派发的数据库提交与额度交接受保护；若请求在提交后才取消，已登记的额度交给同一请求的清理流程，业务正文不会因此启动。独立尾部结算先在写事务之外读取真实上传状态，再用短事务复核并结清；如果没有足够的证明，保留原额度供后续恢复。当时调用者持有未提交业务事务的路径仍在事务内结算，后来改为外层事务结束后结算，见下节。

SQLite 基线显示取消发生在账户扫描时，原版还会创建一条账户额度和一条分块转移，随后把两条结清。修复后 SQLite 和 PostgreSQL 的同一取消点均无新增记录。两个数据库在实际 SQL 提交之后取消时各有一条额度及一条转移，均结清并恢复原 94,208 B 剩余空间。四组都没有发送已取消请求的正文，重试后读回完整内容。499 仅是隔离验收适配器对实际任务取消的观测；生产 API 不增加此协议。[基线](../../tmp/owner-cancel-sqlite-t59yhzwr/owner-cancel.json) · [SQLite](../../tmp/owner-cancel-sqlite-je6oelrf/owner-cancel.json) · [PostgreSQL](../../tmp/owner-cancel-postgresql-xpwjf04n/owner-cancel.json)

结算基线在真实 S3 元数据读取完成后暂停，独立 SQLite 写事务不可开，暂停超过一个 5 秒终端心跳周期后，实际 WSL 续租时间未推进。释放读取后结算和停止命令均完成。[结算基线](../../tmp/owner-cancel-settlement-load-a0bpvkbc/owner-cancel.json)

锁外结算复验在认证 SeaweedFS 和 SQLite 上完成。原请求读取暂停时，六个独立账户实际各写入 94,208 B，共 565,248 B，并各自完成 SQL 分块及账户额度结算。独立写事务可开，真实 WSL 出现持续心跳，租约前进约 20.4 秒。释放后已取消请求的额度结清，原 94,208 B 上传承诺恢复，终端停止经过验证。[并发复验](../../tmp/owner-cancel-settlement-load-lqfhojxy/owner-cancel.json)

复跑：服务器虚拟环境执行 `owner_storage_cancel_acceptance.py`，添加 `--postgres` 验证另一数据库的主动取消；添加 `--settlement-load` 运行认证 S3、SQLite 和实际 WSL 的并发结算。脚本启动前检查无模型调用来源登记。

隔离 PostgreSQL 与认证 SeaweedFS 复跑实际上传取消和清理：两处进程真实退出、五个迟到条件写返回 412、最终占用额度结清。真实 DeepSeek deepseek-flash 两次 HTTP 200，工具读回匹配；模拟 0。首轮并行启动 SeaweedFS 时独立测试端口冲突，尚未进入业务或推理；使用同配置串行启动后的复跑通过。[上传清理](../../tmp/live-cli-startup-jb_9cdkg/upload-cleanup.json) · [模型](../../tmp/live-cli-startup-jb_9cdkg/live-result.json) · [首轮隔离库清理](../../tmp/job-repeat-postgres-mwnoz23b/result.json)

没有新增迁移，未改开发库或正在运行的 Docker 服务。仍需按 RB-14 原范围复核其它写入入口、业务事务内结算及跨运行时组合。

## 2026-11-20 外层业务事务结束后结算

已排空的 S3 额度在调用者仍持有 SQL 时保持完整预留。额度上下文退出只登记结束后的清理：嵌套保存点不触发它；外层提交、回滚或会话关闭释放 SQL 后，才读远端日志并在独立短事务里结算。无法确认远端关闭时额度保留，已经确定的业务提交结果保持。没有新增迁移。

- 原基线在真实远端日志读取时仍持 SQLite 写锁。修复后两种数据库各通过外层提交、回滚和关闭三种情形，并核对嵌套保存点结束不会提前结算。读取暂停时另一账户实际修改返回 200；提交后读到新算例名，回滚和关闭保留旧名。日志证明、实际对象原文及结清额度均已复核。[基线](../../tmp/owner-deferred-sqlite-0vbpcn3a/owner-deferred.json) · [SQLite](../../tmp/owner-deferred-sqlite-sdoks5rc/owner-deferred.json) · [PostgreSQL](../../tmp/owner-deferred-postgresql-dnvq7x48/owner-deferred.json)
- 在 PostgreSQL 和认证 SeaweedFS 上用实际工作区恢复接口恢复了 12,582,912 B 二进制文件，读回一致。错误摘要返回 400，超过归档上限返回 413，两次拒绝均不增加工作区。真实 DeepSeek deepseek-flash 两次 HTTP 200，工具读回一致；模拟 0，隔离数据库已删除。[工作区恢复](../../tmp/live-cli-startup-afi8fpyu/workspace-restore.json) · [模型](../../tmp/live-cli-startup-afi8fpyu/live-result.json)

- 两库分别在业务 SQL 已提交且真实 S3 日志读取暂停时让隔离 API 以 76 退出。重启后读取到账户额度仍活跃、日志未关闭；按原进程退出证据恢复，原额度结清，已提交算例名及对象原文保持。两个隔离库均已结束使用，PostgreSQL 临时库已删除。[SQLite 真实退出](../../tmp/owner-deferred-sqlite-sg4o1hhm/owner-deferred.json) · [PostgreSQL 真实退出](../../tmp/owner-deferred-postgresql-v2rme71x/owner-deferred.json)

- 会话收尾改动后的主动取消复跑通过：盘点时无新增额度，已提交后正确结清，重试读回原文。[取消回归](../../tmp/owner-cancel-sqlite-2xu8bdfg/owner-cancel.json)

复跑入口是 owner_deferred_settlement_acceptance.py，添加 --postgres 选择临时 PostgreSQL，添加 --exit-at-proof 让真实 API 在 SQL 提交之后、远端日志关闭之前退出。专项不含模型推理；正式上传和工作区入口仍按 RB-12、RB-14 原组合推进。
