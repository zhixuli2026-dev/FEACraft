# 原执行缓存恢复：N5-04第二段

## 2026-11-21，恢复部分缺失的运行目录

原尝试取得已归档和进程退出证明后，恢复接口可补齐仍存在的运行目录。现有文件和目录先与归档逐项核对。缺失文件经过节点额度和私有缓存准入，再按大小、SHA-256 和执行位核验。目录和文件采用无覆盖安装，保留现有目录和文件的物理身份。补齐过程由原收尾 I/O 归属保护，并在数据库写事务外执行。提交原恢复回执前，再核对完整目录中的文件版本。 [运行目录校验和安装](../server/app/services/attempt_objects.py) · [收尾归属](../server/app/services/finalizing_io.py)

旧版实测保留运行目录但缺两个产物和一个空目录时返回 409。新版在 SQLite 与认证 SeaweedFS 中只暂存缺失的 262,518 B，原有 33 MiB 成员保持。原有文件等长变更、未登记文件、远端缺失成员等长损坏均拒绝，已有字节保留。PostgreSQL 同时补齐封存输入和运行目录，安装首个输出后让 API 实际退出，重启只暂存剩下的 258 B。两个数据库都能完结原作业，原生程序各执行一次。按原进程退出证据回收私有暂存后，已恢复文件摘要保持。 [旧版对照](../../tmp/attempt-output-recovery-vntlx82t/output-result.json) · [SQLite 33 MiB](../../tmp/attempt-output-recovery-2oyb_om9/output-result.json) · [PostgreSQL 退出重试](../../tmp/attempt-output-recovery-xymlp76c/output-result.json) · [暂存清理](../../tmp/attempt-output-recovery-xymlp76c/partial-output-crash-cache-sweep.json)

SQLite 在安装首个成员后暂停，独立写事务约 0.032 秒提交。真实 API 退出后，重启补齐并完成原作业。原锁范围回归确认 33 MiB 正文的锁内读取为 0 B，同长度同修改时间的改写仍被拒绝。整目录冷恢复和普通作业归档也通过。模型调用和模拟调用均为 0。 [安装中并发及退出](../../tmp/attempt-output-recovery-8hm6kx_x/output-result.json) · [事务外校验](../../tmp/attempt-output-recovery-9s5hu6ip/output-result.json) · [冷恢复](../../tmp/attempt-output-recovery-w2iiklef/output-result.json) · [正常作业](../../tmp/attempt-output-normal-vtsn8d2a/output-result.json) · [阶段汇总](../../tmp/N4-05-partial-runtime-20261121/validation.json)

两个 API 同时恢复同一个部分缺失目录时，一端返回 200，另一端收到 409 `output_recovery_busy`。重试读到同一份恢复回执，数据库只保存一个恢复事件，缺失输出只暂存一次。原作业随后完结，隔离 API 均已退出。 [双 API 回执](../../tmp/attempt-output-recovery-ss42cu22/output-result.json)

## 2026-11-21，恢复部分缺失的封存输入

作业已归档并取得原工作进程退出证明后，`POST /jobs/{id}:restore-output-cache` 可以补齐原尝试内缺失的输入文件、封存清单和目录。恢复核对 SQL 中的尝试身份、S3 原清单和对象集合，并核验本地保留输入的摘要、文件类型和执行位。缺失对象通过现有节点空间准入和四路读取池流式校验。清单单独占用其实际字节额度。每个落盘文件使用无覆盖移动，保留已有内容和原收尾记录。进程中途退出时，同一请求验证已落盘的部分后继续。普通作业启动和结果捕获沿用已有封存校验。 [对象校验与安装](../server/app/services/attempt_objects.py) · [恢复请求](../server/app/services/attempt_cache_recovery.py)

对照中，原尝试保留收尾目录但缺少一个封存输入、清单和运行目录，旧版返回 HTTP 409 `output_cache_unavailable`。新版在真实 SQLite、PostgreSQL 和认证 SeaweedFS 中通过恢复与完结。本地存活文件或 S3 缺失成员的等长损坏均被拒绝，损坏内容保留。PostgreSQL 场景在输入安装后、清单安装前让 API 实际退出。重启后原尝试完成，原生程序总计执行一次，已完结回执可重试。进程自有暂存通过原维护路径回收，已恢复文件的 SHA-256 一致。整目录冷恢复也通过最终完结。 [旧版对照](../../tmp/attempt-output-recovery-v9clbc74/output-result.json) · [SQLite 部分恢复](../../tmp/attempt-output-recovery-4noe5fza/output-result.json) · [PostgreSQL 中途退出](../../tmp/attempt-output-recovery-cap364a8/output-result.json) · [整目录冷恢复](../../tmp/attempt-output-recovery-ei0s8cd2/output-result.json) · [阶段汇总](../../tmp/N4-05-partial-cache-20261121/validation.json)

## 2026-11-21，原输出缓存的事务外校验

缓存恢复在数据库写事务外核验归档文件的完整字节，并在摘要读取前后核对文件身份和目录成员。进入原尝试的写事务后，以已验证的文件版本核对原缓存或执行无覆盖安装。安装后的目录须对应已验证的文件和空目录。Windows 使用 NTFS 实际更改时间核对文件版本，检测相同长度和相同修改时间的改写。校验沿用恢复请求原期限。 [缓存准备和安装](../server/app/services/attempt_objects.py) · [恢复请求](../server/app/services/attempt_cache_recovery.py)

认证 SeaweedFS 与 SQLite 的实际 33 MiB 产物对照中，旧版安装事务内读该产物 34,603,008 B，独立 SQLite 写入约 0.891 秒后失败。新版安装事务内读该产物 0 B，独立写入在锁外摘要期间约 0.016 秒提交。已复核真实同长度、同修改时间的改写被拒绝，改写内容保留，原恢复回执未增加。恢复已知字节后同请求成功。 [旧版实测](../../tmp/attempt-output-recovery-o5g7mjhd/output-result.json) · [新版实测](../../tmp/attempt-output-recovery-ok1sliur/output-result.json)

PostgreSQL 与认证 SeaweedFS 在整个缓存目录已移开的情况下恢复输入和输出。安装完成后让 API 实际退出，重启后验证实际文件并提交唯一恢复回执。第一轮夹具未计入后来新增的 `finalizing-staging`，在冷缓存准备阶段停止。复跑把完整旧目录移进隔离的证据目录，保留其中的文件。SQLite 双 API 回归通过实际取消排空、并发 200/409 和同一回执重试。此专项执行真实作业和存储，模型调用 0。 [PostgreSQL 冷恢复](../../tmp/attempt-output-recovery-8giu3lj1/output-result.json) · [SQLite 取消与并发](../../tmp/attempt-output-recovery-0h4mvryo/output-result.json) · [阶段汇总](../../tmp/N4-05-runtime-cache-lock-20261121/validation.json)

2026-11-10（北京时间）。承接[输出补传归档](attempt-output-upload-recovery.md)，现在能够从完整远端归档恢复原attempt的执行目录，包括整个本地attempt目录已丢失的情况。原生程序不重启，Job/attempt、输入输出清单、原worker及停止历史不改写。主线仍37/53完成、剩16项，N5-04继续进行，295项全功能目标保持。

## 已交付行为

`POST /api/v1/jobs/{job_id}:restore-output-cache`接收原`attempt_id`和`inventory_sha256`，返回`state:cache_ready`、归档记录ID、唯一缓存恢复记录ID及`finalization_pending:true`。只接受已核验归档、原owner确切退出、尚未形成完整排空/发布准备证明的原finalizing尝试。取消、当前attempt变化、写权限撤销、原生子执行历史变化或存储绑定不匹配都会拒绝。

下载前、安装前分别复核完整输出准备证明及原持久事件、根/子进程实际停止、清理历史、活动账户、case写权限与回收站状态。原owner退出沿用已绑定的确切出生身份观察证书。下载过程中不持有case事务；安装及恢复记录在case控制下串行，重新核对原输入/输出/发布/进程等绑定。

`attempt_objects.ensure_inputs(create_run=False)`只冷恢复真实封存输入和清单，不在旧输出位置创建一份新的输入副本冒充执行结果。完整输出进入既有进程归属、文件日志和节点额度控制的私有Cache；逐文件流式核验，保留可执行标记和包括空目录在内的完整目录清单。已有正常运行路径默认仍创建原输入工作副本。

整目录冷缓存使用同一存储根内的无覆盖目录移动。已有运行目录按上文核验并补齐已登记的缺失成员。发现未登记或损坏的文件时保留原数据并拒绝。并发下载的副本在安装时重新检查；两端可收敛到同一目录和唯一 `attempt.output_cache_restored` 事件。重复请求重新核验实际远端和本地字节。目录已安装而 SQL 记录尚未提交时退出，下一次请求检查实际目录并补交记录。

请求取消等待真实下载/安装线程返回，保存已完成的临时对象引用后才清理；不能因取消遗弃写线程。节点预留覆盖下载，安装后物理文件继续计入库存，不产生新的原生执行/日志输出额度。入口复用每API最多2个恢复操作及300秒绝对请求期限；没有声称全局并发/内存硬上限已完成。

初版最终安装在算例锁内核验完整正文，上文的新版将正文核验移出写事务。进程中断遗留的私有缓存沿用确切所有者退出和逐文件日志核验后清理。未知文件保留。N4-05 继续核对其他暂存、恢复和长 I/O 入口。

## 实际证据

全部为实际Python原生进程、随机262144字节输出、真实HTTP、SQLite/临时PostgreSQL及SeaweedFS 4.52。真实模型0、模拟LLM0；没有回复、流、工具选择、Agent回合或CLI推理替身。文件/目录故障与纯请求协议拒绝分别记账。

| `tmp/`证据目录 | 实际场景 |
| --- | --- |
| `attempt-output-recovery-eyh2r9_v` | SQLite：验证远端输入与输出后实际删除整个本地attempt，下载完成但安装前API退出79；重启恢复原输入、随机输出及`empty/leaf`空目录，原生调用仍1次。 |
| `attempt-output-recovery-bged69f_` | PostgreSQL：同样实际删除整个attempt，安装后、缓存恢复SQL事件提交前API退出79；重启保留正确目录并只补交一份事件。 |
| `attempt-output-recovery-qfvl_3ua` | SQLite：仅删除已归档run，实际SDK正文下载暂停后3次真实取消；线程排空且无目录/缓存事件。随后两真实API并发恢复，200/200同事件。 |
| `attempt-output-recovery-_ymoemay` | PostgreSQL：同上实际取消及双API安装收敛。 |
| `attempt-output-normal-7un4bu4r` | SQLite：普通无故障原生作业仍成功，完整停止/排空、归档额度物化正常，没有恢复事件。 |
| `attempt-output-normal-mtmmkvhu` | PostgreSQL：普通无故障兼容同上。 |

每份目录内`output-result.json`保存原始结果。本段6轮均通过，没有失败后改写为通过的记录。前两轮验收入口版本v1，后四轮v2，生产源码相同；导入审查分别为`tmp/N5-cache-rebuild-review-v1-20261110.json`与`tmp/N5-cache-rebuild-review-v2-20261110.json`，不能把不同入口摘要说成相同。

四轮恢复均实际篡改已有`A-output.bin`，入口拒绝且没有覆盖损坏文件；测试恢复原字节后同事件重读。每轮还有错误原attempt/清单的请求拒绝：补传2条、缓存2条，四轮共16条协议负向检查；这不增加整项完成数。

合计6次实际原生程序调用、6次明确API退出故障，16个观察API出生身份有实际退出记录；10个自有API端口另行复核。6份自建S3停止记录、3个临时PG删除及只读不存在复核保留。两轮下载取消共6次`Task.cancel()`，实际HTTP取消结果为500，单独列为故障注入，不写成正常成功响应。

安装前退出留下完整临时文件，安装后退出留下已搬走数据的临时日志。checkpoint只对这两份已知测试容器核对确切原owner退出，再调用原私有Cache恢复器清理，并复核恢复后的原run字节/目录不变；证据见汇总validation，未操作开发存储或未知目录。

## 下一段与原要求

原四条N5-04 `remaining`完整保留。本段完成已证明、已归档原生输出的冷缓存恢复和两处安装中断，不是完整finalizing恢复：

- 继续原专业handler的证据生成/校验、日志同步、完整排空、不可变发布和outbox提交；不得把根进程exit0直接当专业成功。
- 需要显式、持久且受约束的恢复执行归属，只授权已登记的专业finalizer，不重新启动原solver，也不改写原worker/进程历史。
- 沿用原claim绝对截止时间及累计输出额度；缺失历史计数不能按0恢复，恢复请求不能暗中刷新原native预算。到期时记录真实资源失败/未验证，不能冒充恢复成功。
- 继续原两处LaTeX物理停止证明缺失的明确处置，以及未形成证明的证据/同步中断、未归档唯一副本丢失、旧staging和未知恢复目录。

源码包：`platform/backups/mainline-N5-04-output-cache-recovery-20261110.zip`；源码摘要、全部Python AST、原始验收、确切临时容器清理及进程/数据库复核见`tmp/mainline-N5-04-output-cache-recovery-evidence/validation.json`。
