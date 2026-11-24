# 原始 S3 staging 清理的期限与锁范围

## 2026-11-21 发布后的普通暂存清理

普通终端、原生 CLI、Agent shell、可视化会话和旧作业的成功收尾共用逐项删除。操作沿用当前较早的截止时间，单独调用时使用现有 300 秒预算。开始删除前写入清理意图，清单保留到目录实际删除结束，清理意图最后移除。暂存同步在清理意图存在时拒绝读取副本。旧作业启动前的废弃副本使用同一清理实现，记录其尚未同步。终端保留原 5 秒失败重试窗口。

SQLite 与 PostgreSQL 各用认证 SeaweedFS 验证真实终端清理入口、旧作业清理入口和旧作业完整执行。文件删除进行到一半触发调用方期限时，请求排空当前删除，测试入口返回 504，清单与清理意图保留，重新同步返回 409。重试清掉剩余暂存并可重复调用，远端两份实际文件及目录联接目标内容均保持。两库各执行一个真实本地作业，成功发布并清除暂存。非特权 WSL 另在 Linux 文件系统验证逐项停止及目录符号链接目标保留。这些专项没有模型调用。[SQLite](../../tmp/staging-sync-cceo5sxv/staging-transfer-deadline.json) · [PostgreSQL](../../tmp/staging-sync-h1np0wlt/staging-transfer-deadline.json) · [数据库收尾](../../tmp/terminal-agent-postgres-uwtf4sce/cleanup.json) · [Linux 文件系统](../../tmp/staging-posix-acceptance-20261121-133348/evidence.json)

PostgreSQL、认证 SeaweedFS 与非特权 WSL 的完整真实 DeepSeek CLI 回归通过 8 项。实际发布、局部删除、重试、清理完成后立即退出 API 以及重启后恢复均通过。两次真实 `deepseek-flash` 回合，模拟调用 0。上游 HTTP 次数未采集。旧作业另通过正常发布、实际提交后退出和待决回执丢失的冷恢复复验。[真实 CLI 回归](../../tmp/live-cli-staging-recovery-sc_a_jqh/live-result.json) · [CLI 临时数据库](../../tmp/cli-staging-recovery-postgres-vjyfy6t8/result.json) · [旧作业三场景](../../tmp/staging-transfer-evidence-44ljk81e/checks.json)

## 2026-11-21 已提交文件事务的同写入者结算

两次带 SQL 提交计时的完整 PostgreSQL 回归通过。旧版两笔实际普通文件覆盖在提交后各还有 4 次物理 PUT，分别耗时 5.125 和 5.656 秒。新版在当前租约已观察到实际 SQL 提交并排空已确认传输后，重新核对同一个待决清单、SQL 见证、实际正文和条件清理结果，复用起先建立的远端写入栅栏。提交后只需结算记录及清理待决指针对应的 2 次物理 PUT，两笔均为 2.672 秒，完整请求分别还剩 9.766 和 9.875 秒。写入准入期限继续采用原配置。之前超时轮缺少 SQL 提交计时，保留原失败证据。

同一租约成功的旧作业读回了 `committed` 结算凭据。提交后 API 实际退出、首个文件应用后退出和待决清单回执丢失继续由冷恢复验证。各场景按原 SQL 见证保留结果或回滚，未完成工作副本仍可恢复。[旧作业四场景](../../tmp/staging-transfer-evidence-m5u5yna_/checks.json)

新版双库专项复验通过。默认与继承期限排空已开始的 1 MiB 读取，主动取消保留副本，四路繁忙时第五路在创建租约前返回 429。释放后全部五路可正常结算。临时 PostgreSQL 库和两个隔离对象服务均已清理。[SQLite](../../tmp/staging-sync-6ktwj2pd/staging-transfer-deadline.json) · [PostgreSQL](../../tmp/staging-sync-2wngrvrw/staging-transfer-deadline.json) · [数据库收尾](../../tmp/terminal-agent-postgres-jrpusm4p/cleanup.json)

认证 SeaweedFS、PostgreSQL 与非特权 WSL 的两轮完整 CLI 回归各通过 8 项，各有两个真实 DeepSeek CLI 回合，模拟模型调用为 0。实际审批中断、冲突保留、局部清理和清理后的实际 API 退出均有证明。模型在恢复后读取文件，回答原样落库。两轮 API 和对象服务已退出，临时 PostgreSQL 库已移除。[对照轮](../../tmp/live-cli-staging-recovery-588bewh_/live-result.json) · [对照计时](../../tmp/live-cli-staging-recovery-588bewh_/actual-file-write-timing.jsonl) · [新版](../../tmp/live-cli-staging-recovery-xrpxxgdy/live-result.json) · [新版计时](../../tmp/live-cli-staging-recovery-xrpxxgdy/actual-file-write-timing.jsonl) · [临时数据库收尾](../../tmp/cli-staging-recovery-postgres-c7u9ohjj/result.json)

## 2026-11-21 普通回写的统一期限与并发准入

普通终端、原生 CLI、Agent shell、可视化会话和旧作业共用的 S3 暂存回写，从存储租约准入到扫描、正文传输、提交与操作结束都继承同一个截止时间。入口使用现有 300 秒期限，调用方较早的期限优先。目录扫描和正文传输共用现有四路复制池。复制池满时在创建存储租约前返回 `429 / staging_sync_busy`。已经发出的 I/O 排空后才释放复制席位和租约。已形成的事务按原恢复协议结算。

SQLite 与 PostgreSQL 搭配认证 SeaweedFS 的故障专项均通过。实际 3 MiB 暂存文件的首块 1 MiB 已读后被暂停。默认期限和较早调用方期限分别到达后，恢复首块读取时返回 504，后续块未再读取。主动取消等待原扫描返回，未进入远端发布。同一副本保留并成功按 SHA-256 回写。另有四个算例同时占用复制池，第五个请求返回 429，租约记录数未变。排空后五个算例均能保存。两个隔离 API 与对象存储均退出，临时 PostgreSQL 库已删除。本专项的真实模型调用和模拟模型调用均为 0。[SQLite 证据](../../tmp/staging-sync-1hp7rndn/staging-transfer-deadline.json) · [PostgreSQL 证据](../../tmp/staging-sync-dhrkkbj6/staging-transfer-deadline.json) · [数据库收尾](../../tmp/terminal-agent-postgres-4r6zsl3o/cleanup.json) · [专项入口](../server/staging_transfer_deadline_acceptance.py)

旧作业的真实本地进程与 S3 回写另通过正常完成、首个对象应用期间取消、首个对象应用后真实 API 退出并由新 API 回滚三项。取消和退出场景保留工作副本，恢复后没有待决远端文件事务。[旧作业真实进程](../../tmp/staging-transfer-evidence-2_krwmcw/checks.json)

本版 PostgreSQL 真实 CLI 集成复跑通过实际审批中断和冲突保留。处理冲突时，普通 `PUT /file` 恢复远端原版本返回 `504 / file_write_timeout`，单次远端写入序列约 32.4 秒。该轮运行了一个真实 DeepSeek CLI 回合。在验收入口加录正式 SQL 提交时间和结算完成时间，下一次发生超时时可以定位写入阶段。隔离 API、对象存储均已退出，临时库已删除。[真实 CLI 失败记录](../../tmp/live-cli-staging-recovery-x9msorek/live-result.json) · [远端写入序列](../../tmp/live-cli-staging-recovery-x9msorek/actual-capacity-calls.jsonl) · [临时库清理](../../tmp/cli-staging-recovery-postgres-fjnecizc/result.json)

SQLite 搭配认证 SeaweedFS 和非特权 WSL 的同版完整 CLI 复跑通过 8 项。两个真实 DeepSeek CLI 回合覆盖审批中断及重启后实际命令读取，回答原样入库，最终会话与两个 API 的停止均核验。模拟模型调用为 0，CLI 上游 HTTP 次数未逐笔采集。对象服务已退出。[SQLite 真实回归](../../tmp/live-cli-staging-recovery-x2k7bjut/live-result.json) · [对象服务收尾](../../tmp/s3-attempt-store-q4kwnv41/cli-recovery-result.json)

## 2026-11-20 暂存正文复核与写入摘要

S3 事务在发布暂存正文之前会再次核对原文件的大小和摘要。远端物理写入的容量准入也会生成同一正文的大小和摘要。两处读取现在沿用请求的截止时间；超时后排空已在进行的文件读操作，停止读取后续分块，也不进入新的物理写入准入。正常的正文大小、条件写入、摘要和额度凭据保持同一验证规则。

用认证 SeaweedFS 和实际 12 MiB 文件对照，第一块 1 MiB 读取暂停到超过 200 ms 截止时间。旧代码的两处摘要各读完 12 MiB；修复后均在第一块返回后停止，未发布该事务目标。正常时限内同一文件写入后读回摘要一致。此专项为基础设施验收，真实模型 0 次，模拟 0 次。[修复前](../../tmp/s3-attempt-store-9gjrplnv/staging-digest-deadline.json) · [修复后](../../tmp/s3-attempt-store-hfm5jlfk/staging-digest-deadline.json)

正式 HTTP 接口通过认证 SeaweedFS 写入 4 MiB，覆盖为 5 MiB，再分别用实际下载核对字节数和 SHA-256。四次业务请求都返回 HTTP 200。实际 `deepseek-flash` 两次调用完成独立证据回读，模拟 0 次，隔离 API 和对象存储已停止。[实际 API](../../tmp/live-cli-startup-rt3nw0me/rb14-current-s3-writes.json) · [DeepSeek 回读](../../tmp/live-cli-startup-rt3nw0me/live-result.json) · [对象存储收尾](../../tmp/s3-attempt-store-mruyd2vr/job-repeat-run.json)

2026-11-07，接续[恢复快照总期限](recovery-request-deadline.md)。原 staging 清理在 case 写锁内校验归档和扫描全部目录，文件删除循环也没有继承请求截止时间。本次让清理 POST 从请求解析和认证开始共享默认 300 秒，服务直接调用也使用同样预算。

## 实现

首次归档校验和原始目录计划扫描移到写锁之外；拿锁后重新鉴权、检查可写状态，并比较同一退休凭证、恢复快照选择和 staging 绑定，才提交清理意图。删除前的第二次归档校验也在锁外进行，再次拿锁后要求提交的意图没有变化。

实际计划写入、剩余内容核验和删除仍受 case 控制锁串行保护，避免另一个 worker 或 case 管理操作在文件操作没有结束时接管。到期后，已开始的系统 I/O 和受保护提交仍须实际结束；不能把释放锁当作已经停止文件操作的证据。

目录枚举改为 `os.scandir` 逐项读取，在枚举时检查条目数和期限，不再先收集并排序整层目录。计划的规范 JSON 仍按键排序，既有 schema、绑定和前缀恢复规则不变。文件摘要逐块检查期限，计划写入在开始前检查并完成已经开始的同步；每项 unlink/rmdir、根目录和元数据删除之前再次检查期限。一次删除在系统调用中越过期限时保留实际已删除事实，不继续后续项。

公共恢复归档校验原先独立使用 30 秒。现取该值与调用者剩余期限的较早者，在读取前后检查，避免已到期的校验继续读取后续块。该变化同样适用于已有镜像清理、恢复和文件写回中继承了请求期限的调用。

部分删除保留已提交意图、计划及尚存内容，重试仍逐项检查原有身份和摘要；未知或更改的内容不删除。完成提交先于响应实际落库时，即使原请求返回 `409 / staging_cleanup_incomplete`，查询和重试仍返回唯一已提交完成事实，不重复写完成事件。

## 验收方法

显式入口：`live_llm_staging_deadline_acceptance.py --source-profile-id 26 [--postgres] [--archive-read]`。兼容入口为 `--restart`（SQLite）或 `--postgres --concurrency`。执行前审查了真实模型适配、S3/WSL 退休准备、SeaweedFS、迁移、两类旧故障辅助代码和进程退出核验；这些辅助代码只操作实际存储、数据库和进程，没有替换模型回复或推理。

双库均使用实际临时 SeaweedFS 和 WSL，十个位置分别为：首次归档、目录枚举、首次计划、数据库锁等待、意图提交后、第二次归档、计划落盘、剩余文件摘要、首次 unlink 后、完成提交后。每处使用 8 秒测试预算；前两次归档和首次扫描/计划暂停时，独立 SQL 事务必须在 3 秒内取得同一 case 控制行的写锁。首次 unlink 后到期必须恰好只删除一份文件；完成提交后到期仍可查询并唯一重试。

实际 ZIP 读取检查使用超过 2 MiB 的未压缩 ZIP，真实第一块读取完成后延迟 0.25 秒，让继承的 0.15 秒期限到达。工作线程必须以 TimeoutError 停止、关闭实际文件句柄，且没有第二次 read；同时验证没有父期限时的正常完整下载。它属于文件 I/O 检查，不是模型验收。

兼容检查保留九处真实 API `os._exit`：意图提交前后、计划前缀/完成、删除标记前缀/完成、实际 unlink、完成提交前后；另以两个真实 API worker 验证三次取消后仍等待实际线程结束，再由另一 worker 完成唯一清理。损坏计划/标记的注入单列为协议/故障检查。

所有模型验收由真实 DeepSeek 自主选择工具，读取实际保留并恢复到项目中的随机证明，检查流式回复和落库；没有运行历史模拟回归集合或放宽审查登记。

## 证据状态

最终 SQLite：[十位置矩阵](../../tmp/sandbox-recovery-ui-retirement-49cxqvbh/staging-deadline-checks.json)、[真实模型](../../tmp/sandbox-recovery-ui-retirement-49cxqvbh/live-result.json)、[对象存储停止](../../tmp/s3-attempt-store-ccodufk8/staging-deadline-run.json)。11 项实际基础设施检查通过，真实推理 2 次、8,530 tokens，严格字符串读回通过。

最终 PostgreSQL：[十位置矩阵](../../tmp/sandbox-recovery-ui-retirement-faatjg_5/staging-deadline-checks.json)、[真实模型](../../tmp/sandbox-recovery-ui-retirement-faatjg_5/live-result.json)、[临时库与对象存储收尾](../../tmp/staging-postgres-02j_xojt/result.json)。同样 11 项实际检查通过，真实推理 2 次、8,538 tokens，严格字符串读回通过。四个锁外暂停位置的竞争写入均在测试要求的 3 秒内完成；日志计时精度不用于宣称零延迟。

[实际 ZIP 块读取检查](../../tmp/archive-read-deadline-tugio9in/result.json)两项通过：在途读取实际结束，工作线程报告 TimeoutError、句柄实际关闭、读取调用次数恰为 1；正常下载的全部字节保持不变。

SQLite 中断兼容：[九处实际 API 退出](../../tmp/sandbox-recovery-ui-retirement-8dmqjfdo/staging-restart-checks.json)、[真实模型](../../tmp/sandbox-recovery-ui-retirement-8dmqjfdo/live-result.json)、[对象存储停止](../../tmp/s3-attempt-store-72080raa/staging-deadline-run.json)。6 项实际基础设施、3 项损坏元数据协议/故障检查通过；真实模型 2 次、8,592 tokens，严格读回通过。

PostgreSQL 双 worker 兼容：[三次实际取消及唯一收尾](../../tmp/sandbox-recovery-ui-retirement-1hna_l0v/staging-concurrency-checks.json)、[真实模型](../../tmp/sandbox-recovery-ui-retirement-1hna_l0v/live-result.json)、[临时库和对象存储停止](../../tmp/staging-postgres-acqtoy25/result.json)。4 项实际检查通过，第二个 API 已退出；真实模型 2 次、8,538 tokens，严格读回通过。两套兼容检查使用相同 staging 清理实现，公共归档块读取的剩余期限随后追加，并由最终双库矩阵及独立实际 ZIP 检查复验。

归档剩余期限追加前的首次矩阵也保留：[SQLite](../../tmp/sandbox-recovery-ui-retirement-tnkr9tqy/staging-deadline-checks.json)、[PostgreSQL](../../tmp/sandbox-recovery-ui-retirement-rxmd9ibp/staging-deadline-checks.json)，各 2 次真实推理，分别 8,532 / 8,536 tokens，严格读回均通过；首次对象存储和 PG 临时库分别见[SQLite 收尾](../../tmp/s3-attempt-store-kqqeyfbf/staging-deadline-run.json)、[PG 收尾](../../tmp/staging-postgres-hlve6vgs/result.json)。不将首次矩阵计作块读取追加修复的证据，也不重复累加为最终检查数量。

最终及相关兼容覆盖共 34 项实际基础设施、3 项协议/故障、4 项模型内容/工具检查。连同首次矩阵，本轮真实 `api.deepseek.com / deepseek-flash` 推理 12 次，HTTP 全部 200，共 51,266 tokens，模拟 0；六套模型读回均严格匹配证明字符串。六套沙箱的 42 个登记进程均核验退出，六套对象存储已停止，三个临时 PostgreSQL 数据库已删除。退休准备的继承检查单独保留，不重复计入新增覆盖。

## 剩余范围

初始和第二次归档、首次计划已移出写锁，实际删除阶段仍需要锁来防止竞争接管。计划重建、剩余文件核验、操作系统读取/同步和删除若不可中断，仍可能需要在期限外等待实际结束；本次只证明到期后的协作停止与保留事实，不证明系统调用硬性瞬时终止。

继续其他 native request/CLI/文件入口的完整资源生命周期、计划/扫描内存峰值和全局队列/并发额度、未知历史及未完成副本维护。目录权限和大批量完整恢复、真实重启/空间/权限故障、既有迟到交接和偶发退出核验问题仍待。主线保持完成 33/53、剩 20，资源完成 11/17、剩 6，全部 295 项交付目标继续。
