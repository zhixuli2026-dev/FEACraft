# 来源校验占用协议

N5-01作业写入互斥已接通：claim/原生与子执行放行检查占用，取得占用前要求完整排空检查点；真实Job占用后写入窗口已关闭，已排空并行收尾可依次校验，重复发布准备被拒绝。双库各25项、迁移各166项、占用SQLite32项、兼容4/4及最终Pandoc2项通过。旧Job缺证恢复、其余直接写入及真实发布来源/依赖锁外校验继续；head6a4fc1b5e087/36表，开发库和端口8000未动。主线11完成/18进行中/24待开始（剩42），全功能295项范围不变。 详见[Job写入排空与校验准入](validation-job-admission.md)。

N5-01原生会话互斥已接通：终端/CLI/viz/request登记与校验占用共用case控制锁，活动、过期、未知停止及暂存残留阻止校验；真实终端复现窗口已关闭。双库各35项、Agent32/CLI36/PTY44/可视化33/占用32项兼容完成；原始4/5与可视化单套复核1/1分别保留。Job写入、其余直接入口和真实发布锁外校验尚未接通。head5f3eb0a4d976/36表，开发库和端口8000未动；主线11完成/18进行中/24待开始（剩42），全功能295项范围不变。 详见[原生会话与校验准入](validation-native-admission.md)。

2026-11-03，N5-01 的基础协议已实现。它尚未接入实际发布流程；当前 `job_execution.finish` 的可编辑来源和递归依赖校验仍使用原来的算例事务。不能把本文的协议验证当作该长事务问题已经解决，也不能据此关闭 N5-01。

## 已实现的边界

`case_validation_fences` 每个算例最多一行，保存所属 Job/attempt、代数、随机能力令牌摘要、Job claim 摘要及租约。复合外键强制 Job 属于该算例、attempt 属于该 Job；父表新增唯一索引，不重建 SQLite 的被引用表。新 Alembic head 为 `5f3eb0a4d976`，共36张业务表。已有占用历史会阻止降级，不能静默丢弃；开发库没有迁移，端口8000没有重载。

`case_validation.py` 提供短事务内的 reserve、require-current、renew、release，以及自行提交的 `revoke_expired`。reserve 要求精确的 Job claim 和当前 finalizing 状态；最终核验还检查 case、Job、attempt、代数、令牌、claim、状态和租约。令牌不通过正式 API 接受或返回。旧代数或伪造令牌不能验证，也不能释放较新的占用。取消后可以用自己的精确令牌释放占用，但不能再验证成功。

`case_write.coordinate` 在文件版本检查和写入之前拒绝 held 占用。即使其租约过期，也仍然拒绝写入。文件内容读取、版本查询、caseDict读取、上传进度四个已审查调用使用 `content_write=False`，保留原来的算例锁和可用性检查。Job心跳、取消使用控制锁，不经过内容写入拒绝逻辑。其余调用和原生写入者还需继续分类。

## 撤销必须先提交

文件写入不是数据库事务的一部分。若写者在自己的数据库事务里撤销占用，随后改写了文件、数据库事务却回滚，旧占用可能再次存在。因此当前实现禁止写者顺便撤销，也不允许 reserve 自动接管过期的 held 行。

`revoke_expired` 在独立短事务中获取同一算例控制锁并提交 revoked；后续写者再进入自己的事务。写者失败或回滚不会恢复旧占用。它只撤销只读校验资格，完全不授权接管、重放或认定原生进程已经停止。不要在其他 session 已持有该算例锁时调用它。

数据库时钟在取得控制锁之后读取。PostgreSQL 使用实时的 `clock_timestamp()`，避免事务开始时间在等待锁后已经过时；SQLite 使用 `strftime(..., 'now')`。依据：[PostgreSQL 当前时间文档](https://www.postgresql.org/docs/current/functions-datetime.html#FUNCTIONS-DATETIME-CURRENT)、[SQLite 时间值文档](https://www.sqlite.org/lang_datefunc.html#time_values)。续租拒绝已过期或早于上次心跳的时间；已提交撤销的资格不会因时钟恢复而重新有效。这不是对墙上时钟单调性的保证。

## 已有证据

`case_validation_smoke.py --postgres` 在两个真实 API 实例、共享隔离存储和每种隔离数据库下各通过32项，证据为 `tmp/case-validation-evidence-_jkm27y2/result.json`：

- 已提交占用阻止另一个 API 修改文件，未提交占用和已释放占用允许写入；无关算例正常写入，四种文件查询及Job查询可用。
- Job心跳、取消可以提交；取消不擅自释放未排空的校验。精确令牌可以在取消或claim变更后只做清理。
- 伪造令牌、case、attempt、代数、较旧占用均不能获得校验权限；两个API争抢同一算例只有一个成功。
- 过期 held 行仍拒绝写入；单独提交撤销后，实际文件写入即使数据库回滚，旧校验也不能在时钟恢复后复活。也覆盖时钟回拨后撤销和新代数保护。
- 11项数据库约束验证包括跨算例/跨Job归属、期限顺序、状态及结束时间；保留历史的降级拒绝、带数据前版本往返和metadata零差异通过。

完整现有迁移专项 `tmp/workflow-migration-6tx678ra/result.json` 在双库各166项通过。当前源码下的迁移专项先达到相应迁移的精确前版本，再测试旧worker身份记录的降级保护，避免SQLite非事务DDL与PostgreSQL的回滚差异混淆结果。

现有兼容回归4/4通过：完整并发写入SQLite93项、作业认领、上传线程排空、工作流状态。汇总为 `tmp/case-validation-compat/summary.json`；完整并发写入证据为 `tmp/case-write-evidence-d0y1v3cs/result.json`。

前三次专项失败均来自新夹具，保留 `tmp/case-validation-dual-first.log`、`tmp/case-validation-dual-second.log`、`tmp/case-validation-dual-third.log`：分别是显式SQLite URL不符合既有测试保护、上传请求误用total_size、rollback后读取过期ORM对象。没有放宽测试数据库保护；成功运行见 `tmp/case-validation-dual-fourth.log`。

## 接下来的必需工作

1. 在授予校验资格的同一控制锁内，证明已知终端、CLI、viz、Agent shell和遗留直接Job写入者已停止。新的登记、放行、暂存回写和直接写入调用必须检查占用；现有 `coordinate` 检查不能代替这些工作。不能把应用自己的原生写者当作“外部绕过”。
2. 在真正的发布来源/依赖读取前短事务 reserve 并提交；锁外读取完整文件哈希和依赖图；最终短事务重验完整绑定、权限、head与图状态及占用后提交并释放。任何取消路径都先排空读取线程，再释放；未知提交先对账。当前没有正式入口创建占用。
3. 把续租、失效处理及独立撤销接入实际生命周期，验证真实Pandoc作业慢来源/依赖读取期间心跳和跨API取消；本专项直接创建finalizing测试记录，没有启动原生作业。
4. 输入封存和别名修复包含写操作，另行设计恢复协议，不能直接套用只读占用的过期规则。完整远端S3、旧版API并存、跨主机和任意原生写入也未由本机测试证明。

主线仍53项（11完成、18进行中、24待开始，剩42）；全功能仍295项，整项完整验收标记0。正式SIWC联调按用户决定暂缓，其余目标继续。
