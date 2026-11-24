# 收尾缓存与日志操作的持久归属

首次完整收尾前无历史IO的新版取消、旧格式诊断及未归档取消的边界见[版本化恢复协议](versioned-finalizing-protocol.md)。本页历史运行、失败和原范围保持。

2026-11-10（北京时间）。承接[独立校验取消收尾](validator-job-cancellation.md)，将归属登记提前到缓存重建、日志上传之前，覆盖校验完成后、提交发布之前的缓存操作，并接入独立的恢复缓存入口。主线仍为 **37/53，剩16项**，资源预算11/17、剩6块。原 N5-04 四条 remaining 与295项全功能目标保持。

## 已复现的问题

`tmp/professional-finalizing-2ftt2jkk/professional-result.json` 在真实报告构建、S3归档和原API退出后，下载并校验实际缓存，暂停仍受保护的工作线程。正式取消原Job后，原attempt停在cancelling；此时还没有NativeRequest，旧收尾无从证明缓存/日志操作已经结束，因此返回409，未形成v5收尾证明。原构建只运行一次，反例记录保留。

后续终态故障测试 `professional-finalizing-0w08l5ys` 又复现：成功终态已提交，API在别名补写及兼容可见状态落库前退出；重复请求虽返回succeeded，却仍带finalization_pending=true，界面继续显示进行中。该失败也保留，没有把“已提交成功”当成“完整收尾成功”。

## 持久互斥与实际结束证明

`finalizing_io.py` 使用现有CaseStorageOperation，purpose为recovery，原attempt绑定到journal_id，冻结绑定摘要放在plan_sha256。`job.finalizing_io_started` 与行在同一事务提交，早于任何缓存下载、安装、日志上传或后续发布操作；绑定原Job、attempt、case前缀、输入/原worker/实际停止/输出清单和实际API出生身份。无需新表或迁移。

整个恢复请求持有该归属，现有Case控制和原生准入只接受这个上下文的确切操作。另一个API在原归属仍存活或状态不明时得到409/output_recovery_busy；超时或租约过期都不能授予接管权限。其他文件操作不会被误认或结算为本次收尾。

所有受保护线程返回、缓存view清理完成、原生作用域结束后，记录`job.finalizing_io_drained`。这只证明该操作的本地IO已结束，不代替原生stop或远端写入完成证明。API中途退出则必须实际观察确切原出生退出，再记录`job.finalizing_io_owner_exited`；保留原操作与独立观察者，未知/不匹配不推断退出。

日志预留处于active或released时，仅在匹配真实IO归属/结束证明的情况下允许恢复观察；原生放行与成功收尾仍要求真实完成的日志回执。日志ticket仅保存claim_sha256，内部fencing值不写入事件。未知远端写入继续保留原容量；缺少对象不算释放或写入失败。受保护请求已经结束时，可按确切日志绑定继续相同摘要、相同条件键的写入。

独立`:restore-output-cache`入口使用同一归属锁，不能绕过完整收尾请求的排他操作。API退出、实际任务取消、损坏缓存拒绝和单次恢复回执均有真实运行证据。

## 登记前取消和终态补交

原Job在预作业期间取消时，先等待实际预作业返回。确切归属证明来自运行中登记的操作，或已核验结束的同一尝试历史；原worker实际退出也必须核验。随后记录取消专用的失败NativeRequest：error为OriginalJobCancelled、无child、prepared为空、native_execution_started=false，用量保留真实原始信用。这是实际取消收尾，不是校验成功结果，也不启动编译器或求解器。继续使用原v5实际停止、原attempt终态和失败发布事务。

成功终态已提交而兼容状态仍pending时，恢复入口接续既有`publication_alias_recovery`，按原不可变发布和别名计划补写。v5分支额外核验独立校验请求、用量和原owner-exit完整历史；使用已持久化的实际退出证明。当前发布已被替代或目标已编辑时，保留新状态，不恢复旧字节。补写完成后持久保存实际可见状态，旧IO归属也被核验回收。此路径不重建执行、不新增校验请求、不重跑编译或求解。

## 实际验收

目录前缀为`tmp/professional-finalizing-`，回执`professional-result.json`；独立缓存两组使用`tmp/attempt-output-recovery-`及`output-result.json`。

| 场景 | 数据库 | 目录后缀 |
|---|---|---|
| 缓存已下载、线程仍受保护时取消Job | SQLite | _6sm68jb |
| 日志预留后API实际退出，双API接续 | PostgreSQL | gy3kkpmf |
| 缓存已下载后API实际退出，双API接续 | SQLite | qwa7t7an |
| LaTeX实际日志PUT返回后取消Job | PostgreSQL | 7_rjaq9b |
| 独立缓存安装前API退出、冷缓存恢复及双API竞争 | SQLite | kshck4ir |
| 独立缓存实际任务取消、重试及双API竞争 | PostgreSQL | 4jtolxgj |
| 真实编译、实际错误后HTTP断连、双API恢复与真实模型 | PostgreSQL | viq8yh93 |
| 新归属锁下运行中原Job取消兼容 | PostgreSQL | f9db_asn |
| 报告终态提交后API退出、别名补交和归属回收 | PostgreSQL | q6oausg4 |
| LaTeX终态提交后API退出、别名补交和归属回收 | SQLite | lio84qhb |

每轮原构建各一次，所有接续保留原Job、attempt、输入、实际停止和绝对截止时间。实际不同API竞争只由合法归属执行；重复收尾返回相同终态，不生成额外原构建或关联校验。独立缓存任务取消组由夹具实际取消服务器任务并解除真实下载线程暂停，观察到HTTP500；该故障响应原样记录，随后真实重试成功，不把500称为正常完成响应。

真实模型使用显式入口`live_llm_mixed_finalizer_acceptance.py --source-profile-id 26 --postgres --two-apis`，`api.deepseek.com` / `deepseek-flash`共2次HTTP200、2份usage、8700tokens。模型真实选择一次read_text_file读取恢复的原源文件，真实后续流式回复返回只存在于文件中的随机验证串。模拟LLM0。该行绑定运行时v4审查；后续终态补交改动另以v8双库实际退出算例验证，不改写旧运行摘要。

## 协议核验和失败保留

`tmp/N5-finalizing-io-readonly-audit-20261110.json` 在两个真实已结束SQLite库上，使用mode=ro/query_only并固定原隔离S3配置验证3份IO结束/退出证明；9次仅内存中的摘要、操作类型或归属改动均被拒绝，数据库字节不变。该项是纯协议检查，模型0、原生执行0，不算真实模型验收。

保留的运行失败：`8rz9f12z`是新代码将内部fencing字段交给事件校验器而被拒绝，已修为摘要；`l3_u1ku3`是验收仍期待旧接管错误码，在实际Job取消前失败，按实际active归属更新预期后重验；`0w08l5ys`是终态pending的真实反例。首次只读核验因未固定原S3配置而摘要不匹配，记录在`tmp/N5-finalizing-io-readonly-failure-20261110.json`，修正核验环境后通过。失败未回写为成功。

源码及导入辅助代码审查为`tmp/N5-finalizing-io-review-v1..v8-20261110.json`。未执行历史混合模拟LLM回归集合。API、S3 owner、临时PostgreSQL库及源码/AST的逐项核验见`tmp/mainline-N5-04-finalizing-io-evidence/validation.json`；API/S3退出不用于补造旧阶段缺失的原生kernel停止证明。开发数据库未写入。

最终实际对账：10组产品场景通过，3份运行失败和1份修复前反例保留，原构建14轮各一次；50个已观察实际API有确切退出记录，14个自建S3 owner实际退出，9个临时PostgreSQL库独立查询确认不存在。另验证13份IO结束/退出证明、22份实际独立原生作用域关闭回执。两份只读协议数据库的9次负面检查单列，不计为新增产品功能或模型调用。

## 仍需继续

没有任何预作业归属或独立请求历史、且已在首次恢复前取消的记录，仍缺少排除历史未跟踪IO的证明，不能猜测已经排空。继续补齐新记录的版本化协议和旧无证明记录的明确诊断/人工处置。

继续原diagnostic增长/未知输出额度耗尽/原期限到期组合、证据生成和同步在完整证明形成前中断、历史缺失物理停止证明的明确处置，以及其他处理器、FS、legacy和旧缓存/staging。旧偶发SQLite锁的精确持锁者仍未定位。N5-04尚未整项完成，53项主线和295项全功能目标均保持有效。
