# 上传创建、完成与取消的所有者额度

## 2026-11-20 S3 上传取消

普通取消、按创建键取消和人工确认取消共用持久算例租约。取得租约前核对可能由已退出分块进程留下的账户额度。短 SQL 事务确认账号和存储绑定、原回执状态及已登记分块偏移。账户批量预留仅用于写取消元数据，保留既有精确字节恢复证明。随后释放该预留，在 SQL 事务外分批关闭实际对象，为已登记但尚未出现的分块写入零字节关闭标记。物理 PUT 逐次登记，全部关闭后才结清上传预留和人工确认回执。

| 验收 | 结果 | 证据 |
| --- | --- | --- |
| 修复前 | 真实认证 SeaweedFS 有 36 个暂存对象。首批 32 个关闭，暂停第二页时独立 SQL 写锁失败，同账户另案保存等待。释放后原取消和另案保存均完成。 | [基线](../../tmp/live-cli-startup-a1ruk4l7/upload-cancellation-lock.json) |
| 修复后 | SQLite 和 PostgreSQL 分别在相同暂停点取得独立 SQL 写锁。同账户另案保存成功，读取看到新内容，全部 36 个对象最终关闭且上传额度归零。 | [SQLite](../../tmp/live-cli-startup-9z6ckfb3/upload-cancellation-lock.json) · [PostgreSQL](../../tmp/live-cli-startup-1ht1hxnq/upload-cancellation-lock.json) |
| API 退出恢复 | 每种数据库 4 次真实进程退出，普通、按创建键和人工确认的取消均可重试。丢失响应的零字节写入凭同键、内容类型和 ETag 结清，每组 6 个迟到请求返回 412。已取消回执可重放，已发布结果保留。 | [SQLite](../../tmp/live-cli-startup-jyws6nwc/upload-cleanup.json) · [PostgreSQL](../../tmp/live-cli-startup-301j9mgg/upload-cleanup.json) |
| 本地文件系统兼容 | 完成和取消的 4 组进程退出后，原路径、未知文件保留和上传额度结清均通过。 | [本地回归](../../tmp/live-cli-startup-o46xuz23/upload-cleanup.json) |
| 创建请求和浏览器兼容 | 原创建回执丢失、尚未建立远端元数据时的取消、无写权限的成员被拒绝均通过。用本工作区修订后的构建实际续传 17,825,805 B，浏览器发出一次创建请求，发布摘要和多段 Range 读回一致。 | [兼容与恢复](../../tmp/live-cli-startup-cotrttyk/upload-creation.json) · [浏览器](../../tmp/live-cli-startup-cotrttyk/browser-upload.json) |

首次恢复复验在已退出分块留下的账户池尚未对账时等待超时。加入写入前对账后，两种数据库均通过实际退出恢复。[首次 SQLite 记录](../../tmp/live-cli-startup-luifc_6u/live-result.json) · [首次 PostgreSQL 记录](../../tmp/live-cli-startup-v0h3silj/live-result.json)

真实 `deepseek-flash` 本轮共调用 18 次，修复前基线 2 次，第一轮并发对照 4 次，最终复验 12 次。全部返回 HTTP 200，模拟 0 次。浏览器第一次缺少 Playwright Python 路径，补齐后旧的 `platform/web/dist` 构建停在浏览器操作。最终通过的是本工作区近期生成的上传修复构建。[最终真实模型回读](../../tmp/live-cli-startup-cotrttyk/live-result.json) · [旧浏览器运行](../../tmp/live-cli-startup-2vyje1h8/live-result.json)

验收前后 C 盘可用空间 22.04 GiB 和 22.02 GiB，Docker 构建缓存均为 22.67 MB。Docker 内部回收和 Windows 实际释放均为 0。定时维护上次运行返回码为 0。

## 2026-11-20 S3 上传后台清理

后台分页读取 SQL 完成回执后结束事务，再核验已提交清单、上传元数据和按 ETag 固定的分片正文。申请持久算例租约后，短事务重新确认账户、存储前缀和完成回执。分片关闭和分页游标使用条件写入。对象请求在 SQL 写事务外执行，物理 PUT 逐次登记额度。重启后依照原游标重扫，已关闭的零字节分片保留。

| 验收 | 实际结果 | 证据 |
| --- | --- | --- |
| 修复前，SQLite 与认证 SeaweedFS | 暂停后台分片校验后，同算例保存等待 9 秒未完成。该次停在存储验收，真实模型请求为 0。 | [失败记录](../../tmp/live-cli-startup-hzi7_o20/live-result.json) |
| 修复后，SQLite 与认证 SeaweedFS | 8 个真实上传。分页枚举、超过 1 MiB 的分片校验暂停期间，同算例和同账户另案各保存成功。API 在首个条件写入后实际退出，重启从原分页继续。同步改写的分片触发 ETag 冲突并保留原样，未完成上传、异常元数据和其他账户数据保留。 | [存储验收](../../tmp/live-cli-startup-15jq4fgy/upload-child-maintenance.json) · [真实模型回读](../../tmp/live-cli-startup-15jq4fgy/live-result.json) |
| 修复后，PostgreSQL 与认证 SeaweedFS | 相同 8 个上传和两组并发写入通过。退出恢复、旧 ETag 拒绝及已发布正文摘要比对通过。 | [存储验收](../../tmp/live-cli-startup-m39uyl7e/upload-child-maintenance.json) · [真实模型回读](../../tmp/live-cli-startup-m39uyl7e/live-result.json) |
| 本地文件系统回归 | 7 个真实上传。分页继续、退役目录中断恢复、异常文件保留、并发写入和发布正文读回通过。 | [存储验收](../../tmp/upload-maintenance-infrastructure-0e41u8yt/upload-child-maintenance.json) |

两组 S3 验收各调用真实 `deepseek-flash` 2 次，4 次均为 HTTP 200，确认实际工具读回和持久消息。文件系统回归为基础设施检查。全程模拟模型调用为 0。

验收前后 C 盘可用空间为 22.04 GiB 和 22.02 GiB，Docker 构建缓存均为 22.67 MB。Docker 定期维护上次完成于 21:00，返回码为 0。此轮 Windows 实际释放空间为 0。

## 2026-11-20 本地上传完成与后台清理

本地 `/uploads/{id}:complete` 在数据库写锁外核对暂存正文，分块复制到同卷文件事务的日志和安装位置。账户盘点也在写锁外完成。发布事务重新核对账户、算例、上传登记、人工确认的来源、目标预期版本和暂存文件标识。事务原子记录文件发布、完成回执及额度结清。正常清理和后台维护提前验证正文，进入算例锁后只复核回执与文件版本、退役目录并删除已知文件。未知文件和无法验证的副本保留。

| 验收 | 实际结果 | 证据 |
| --- | --- | --- |
| SQLite、PostgreSQL 与 Windows 文件系统 | 各暂停校验、复制和清理。同账户另一算例写入均返回 200。暂存读期间取消获胜后没有发布目标。并发修改目标时，完成返回 409，保留新版本及原暂存。 | [PostgreSQL](../../tmp/live-cli-startup-ws4awnvk/upload-completion-lock.json) · [SQLite](../../tmp/live-cli-startup-3_rah0dl/upload-completion-lock.json) |
| 提交前后真实进程退出 | 两库各两次。提交前在文件已安装时退出，重启用文件事务恢复。提交后、清理前退出，重试得到同一回执。两种场景均验证读回摘要。 | [PostgreSQL](../../tmp/live-cli-startup-ws4awnvk/upload-completion-lock.json) · [SQLite](../../tmp/live-cli-startup-3_rah0dl/upload-completion-lock.json) |
| 40 MiB 本地组装 | 节点上限不足时返回 409，未创建目标，上传进度保留。恢复节点余量后完成并读回 41,943,040 B。 | [节点额度](../../tmp/live-cli-startup-ly19y16u/upload-completion-lock.json) |
| 旧式清理与后台维护 | 每库四次清理中断恢复。后台进程重启后继续按页处理，暂停实际正文校验时同案及另案都完成写入，未知副本和其他用户上传不变。兼容复验覆盖已取消上传的原 409。 | [PostgreSQL 清理](../../tmp/live-cli-startup-v17qx7xv/upload-cleanup.json) · [SQLite 清理](../../tmp/live-cli-startup-5gygv9ai/upload-cleanup.json) · [PostgreSQL 维护](../../tmp/live-cli-startup-tp_1ra5j/upload-child-maintenance.json) · [SQLite 维护](../../tmp/live-cli-startup-99y94z9n/upload-child-maintenance.json) |
| 浏览器 | 修复项目面板和可视化面板的同级 React 重复键。两库均只显示一个上传控件，只发送一次创建请求，恢复后读回完整 17,825,805 B。旧浏览器三套兼容检查共 19 项通过，含刷新后保留之后的编辑。 | [PostgreSQL](../../tmp/live-cli-startup-6vp3dhns/upload-creation.json) · [SQLite](../../tmp/live-cli-startup-96jpkq47/upload-creation.json) · [兼容结果](../../tmp/live-cli-startup-fprapqmb/upload-browser-compat.json) |

实际 S3 上传完成复验继续通过。合计 13 轮独立验证，每轮使用真实 `deepseek-flash` 调用工具读回证据，两次 HTTP 200，共 26 次，模拟 0 次。保留已取消上传一度误返 404 的失败，以及修复重复键前的浏览器失败。修复重复键后，旧兼容套件有一轮组合断言失败，实际磁盘仍是后续编辑。拆分请求数和读回状态的断言后复验通过。本页首节记录 S3 后台清理的后续锁范围修复。[S3 复验](../../tmp/live-cli-startup-l1bdbg61/upload-completion-lock.json) · [旧 404](../../tmp/live-cli-startup-zvatpr5_/live-result.json) · [浏览器重复控件](../../tmp/live-cli-startup-py2dl0iy/browser-failure.json) · [旧组合断言](../../tmp/live-cli-startup-iz3bdd7p/upload-browser-failure.json)

## 2026-11-20 S3 上传完成的锁范围和组装副本

`POST /uploads/{id}:complete` 使用持久算例存储租约保护当前上传。账户用量在 SQL 写锁外盘点，然后在短事务内核对账户版本和上传进度，校验完成所需额度。远端分块组装、条件发布与分块清理在数据库事务外执行。实际 S3 PUT 继续逐笔登记所有者额度，文件提交见证、完成回执和上传额度结清由同一事务提交。每次短事务都重新核对发起账户的算例权限和租约归属，文件发布核对原目标版本。

组装正文和进程身份标记合计预留声明正文大小加 4,096 B。按原身份确认进程已退出后，同一上传的重试可回收遗留本地组装副本。没有归属标记或无法确认的目录继续计入节点空间。纯 S3 算例首次完成时可以没有本地缓存根，只有通过节点空间检查后才创建它。

| 验收 | 实际结果 | 证据 |
| --- | --- | --- |
| 旧版对照 | PostgreSQL 下，真实分块组装暂停时同账户另一算例的写入返回 409。解除暂停后原上传成功，正常回执与读回仍有效。 | [旧版对照](../../tmp/live-cli-startup-d26ld2jo/upload-completion-lock.json) |
| 两库与认证 SeaweedFS | 组装、发布、清理暂停时，同账户另一算例持久写入均返回 200。组装中的同案取消被租约拒绝。2 MiB 正文及完成回执读回匹配。 | [PostgreSQL](../../tmp/live-cli-startup-vsn8rre4/upload-completion-lock.json) · [SQLite](../../tmp/live-cli-startup-uuwlz9mu/upload-completion-lock.json) |
| 真实 API 退出 | 两库各两次。发布后提交前以 79 退出，提交后清理前以 81 退出。重启重试最终返回同一完成回执，并读回对应正文。原组装目录带进程身份证明并被回收，同一父目录中的未知副本仍保留。 | [PostgreSQL](../../tmp/live-cli-startup-vsn8rre4/upload-completion-lock.json) · [SQLite](../../tmp/live-cli-startup-uuwlz9mu/upload-completion-lock.json) |
| 40 MiB 节点峰值和坏块 | 160 MiB 竞争预留耗尽节点余量时，完成请求在组装前返回 409。真实坏块读入 8 MiB 后拒绝，原块及目标保持。正常写入读回 41,943,040 B，峰值预留 41,947,136 B，完成后为 0 B。 | [节点验收](../../tmp/live-cli-startup-s947r97n/upload-assembly-budget.json) |
| 本地文件兼容 | Windows 文件系统与 PostgreSQL 的清理回归通过，含原上传取消和已发布文件的 4 次清理中断。 | [本地兼容](../../tmp/live-cli-startup-9i69rleb/upload-cleanup.json) |

最新两库、节点额度和本地兼容的四套证据分别由真实 `deepseek-flash` 读取，合计 8 次 HTTP 200。旧版及改动过程的三轮各有独立回读，共 6 次 HTTP 200。模拟 0。首轮引入副本归属后发现无本地缓存根时返回 500，修复后的两库均复验通过。[采集入口](../server/live_llm_upload_completion_lock_acceptance.py) · [首轮 PostgreSQL](../../tmp/live-cli-startup-2025y9eg/live-result.json) · [首轮 SQLite](../../tmp/live-cli-startup-c_s_2a22/live-result.json)

Windows 本地文件系统与 PostgreSQL 的普通上传完成对照也已复现。读取真实 2 MiB 暂存文件的校验暂停时，同账户另案的 SQL 写事务返回 409。解除暂停后原上传完成，幂等回执和正文匹配。该路径的锁范围继续由 RB-14 推进，此对照另有 2 次真实 DeepSeek HTTP 200，模拟 0。[本地锁范围对照](../../tmp/live-cli-startup-ydd9rwgl/upload-completion-lock.json) · [真实模型回读](../../tmp/live-cli-startup-ydd9rwgl/live-result.json)

2026-11-06：三个上传写入入口在进入业务事务前取得工作区所有者的持久写入额度，提交业务状态、完成必要清理后才结算。分块写入的额度转移仍是后续工作，本批只修正它的计费所有者。

## 本次实现

创建上传现在先检查声明大小，再在当前所有者额度中保留相同大小的未来内容空间。`Scope.reserve_future` 只能在远端写入开始前调用，元数据、恢复栅栏和写入日志均不能动用这部分空间；它们还必须留下关闭日志的余量。创建成功后，SQL 上传预留承接未来字节，所有者额度随后结算。创建失败不会返回成功的会话承诺，已产生的元数据按实际用量保留。

完成上传时，额度覆盖协调恢复、完成清单、文件事务、数据库提交及暂存清理。清理失败仍返回已有的 `cleanup_pending`，下一次完成请求可凭持久提交记录重试。取消上传也在完整额度范围内修改远端状态和 SQL 预留，重复取消保持幂等。

新增 `owner_for_case` 供授权后的计费使用，`hold_case` 复用相同解析逻辑。创建、分块、完成、取消以及上传列表统一按工作区所有者的预留处理。共享成员访问仍先通过原有权限检查，成员个人额度不替代存储所有者的额度。

## 实际验收

显式入口 `platform/server/upload_write_capacity_checks.py` 使用真实 S3、隔离 API，以及 SQLite/临时 PostgreSQL。每库 11 条检查，共 22 条；没有 LLM 调用或模拟推理。

- 零额度拒绝创建，不增加远端字节或上传预留。
- 声明内容几乎占满剩余空间时，在写入前拒绝，不能挤掉恢复余量。
- 1 MiB 限额下成功创建较大上传，实际物理字节加未来预留不超过限额。
- 取消后释放未来预留；额度不足时取消请求先拒绝，不改变现场，恢复额度后可取消及重复取消。
- 已收齐上传在额度耗尽时拒绝完成，不新增写入。
- 注入实际清理失败后，已提交文件仍可凭完成记录重试清理，下载内容与原始 32 KiB 字节一致。
- 共享成员在个人存储额度为零时，依所有者额度完成创建、分块、列表查询和完成；SQL 预留归属于所有者。
- 所有者额度为零时，成员无法绕过限制；观察到的三个写入范围都持有所有者额度。

证据：[SQLite](../../tmp/upload-write-capacity-01jq_66u/checks.json)、[PostgreSQL](../../tmp/upload-write-capacity-it_r3s5p/checks.json)、[临时库已删除](../../tmp/mixedread-postgres-jiassbqf/cleanup.json)。日志中的 `intentional isolated cleanup failure` 是本次注入的清理故障，已核对重试成功，未将失败清理当作完成清理。没有修改开发数据库或重启开发服务。

## 2026-11-19 S3 完成上传的本地组装额度

`POST /api/v1/cases/{case_id}/uploads/{upload_id}:complete` 在组装远端分块前，按已核验的上传大小预留本地节点空间。预留覆盖组装、文件事务使用和本地清理。预留不足时返回 `409 node_storage_full`，原远端分块、上传进度和已有目标保持。远端所有者额度、整件 SHA-256、条件替换和 SQL 提交见证仍沿用原有校验。 [组装与清理](../server/app/services/s3_uploads.py) · [正式验收入口](../server/s3_upload_assembly_budget_smoke.py)

实际 SQLite 和认证 SeaweedFS 测试使用 5 个 8 MiB 分块。节点限制为 192 MiB，另一个真实节点占额持有 160 MiB。旧版先完整写入 41,943,040 B 的本地文件，后续额度检查才返回 409。修复版返回同一错误码时，实际组装字节为 0，临时目录尚未创建。两个版本的原目标及五个已上传分块均保留，释放竞争预留后可以用同一上传会话重试。 [旧版回执](../../tmp/upload-assembly-budget-s3-9tv2fqnr/upload-assembly-budget.json) · [修复版回执](../../tmp/upload-assembly-budget-s3-7yf6hgrx/upload-assembly-budget.json)

修复版一个同尺寸坏块在实际写入 8 MiB 局部组装后被拒绝。清理开始时仍持有整件 40 MiB 的预留，删除临时目录后归零。原目标和上传会话保持。恢复夹具中被替换的分块后，同一会话成功写入 40 MiB，实际下载的 SHA-256 一致。正常清理前的组装文件和预留各 40 MiB，清理后均为零。重复完成返回同一结果，未再次组装。 [执行器](../../tmp/vibeflow-regression-all-20261119-192413-93fac1/summary.json)

本地删除若报错，沿用原错误返回。已发 I/O 排空后释放未来增长预留，仍存在的实际文件继续由节点清单计量。删除失败行为按现有实现与节点计量规则核对，本次坏块验收实际成功删除。两轮验收的 API、SeaweedFS 进程和监听均已独立确认退出。测试数据在 D 盘，实际空闲空间为 C 盘 22.56 → 22.55 GiB、D 盘 377.47 → 376.98 GiB。模型调用为 0。 [节点计量](node-storage-admission.md) · [旧版执行器](../../tmp/vibeflow-regression-all-20261119-191556-fdc51f/summary.json) · [隔离存储](../../tmp/s3-attempt-store-mhw7wdfa/store.json)

## 剩余要求

分块写入尚未接入持久额度转移。其现有未来字节预留不能先释放再裸写，也不能在普通所有者额度中重复要求同样大小。后续需要把本次分块可消耗的预留与写入身份持久绑定，确保进程死亡、远端请求结果未知、重复块以及取消交错时，未证实的空间仍被占用。元数据和恢复日志不能消耗尚未物化的内容预留。

历史上按共享操作者登记的上传预留没有在本批自动改写；身份不匹配仍拒绝，需要单独核验绑定与现场后处理。未重跑 FS 全矩阵、跨 worker 并发和服务器崩溃组合，不把本批证据扩展为这些场景已通过。

主线仍完成 33/53、剩余 20 项；资源预算完成 11/17、剩余 6 块。RB-12、后续资源块与全部 295 项功能交付仍继续，22 条检查记录不作为新增完成任务数。
## 2026-11-19 遗留上传分块的分页关闭

一般 S3 上传取消通过已持久化的取消元数据阻止后续正式写入。分块位于会话的私有对象前缀，按物理对象每页 32 项读取，并用当前 ETag 将每个非空对象替换为零字节关闭标记。这样可阻止迟到的条件写入。收到中断时保留已关闭的键和原额度，下一次请求跳过现有关闭标记并完成其余工作。[生产实现](../server/app/services/s3_uploads.py) · [验收入口](../server/upload_cancel_pages_smoke.py)

认证 SeaweedFS 的旧版对照有 70 个实际对象，包含正式接口写入的分块及未登记的遗留分块。旧版先列出全部对象。固定版在读取第二页前已关闭 32 个对象。此时注入一次请求中断，返回 503，实际 32 个关闭标记和 8,178 字节仍在用的额度均保留。重新启动隔离 API 后，用同一会话取消，跳过已关闭对象并关闭剩余 38 个，结清 SQL 额度为 0。取消的上传没有发布目标文件，同算例的另一文件内容保持。两个对象存储进程及 16 个监听均已退出，隔离 API 端口关闭。实际模型与模拟调用均为 0。[旧版](../../tmp/upload-cancel-pages-a3abt924/cancel-pages.json) · [重启对照](../../tmp/upload-cancel-pages-mk8zjgsg/cancel-pages.json) · [独立退出核查](../../tmp/upload-cancel-pages-mk8zjgsg/listener-exit-proof.json)

