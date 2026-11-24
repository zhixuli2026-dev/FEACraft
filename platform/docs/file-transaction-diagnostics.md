# 未结文件事务诊断与自动恢复

2026-11-20。对应 N4-05 的 RB-12 和 RB-14。

## 普通本地文件保存

本地文件的普通保存、表单上传和删除先使用进程私有节点额度，准备前像、后像和待安装文件。这段读写在 SQLite 或 PostgreSQL 的业务写事务开始前完成。最终提交锁内重新授权，复核算例来源、文件身份、请求修订、SHA-256 条件和账户额度，安装文件并提交原事务见证。保留原日志和回滚格式。Windows 使用文件属性中的变更时间校验现存文件。生产恢复和待决诊断继续读取既有格式。[Windows 文件属性](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_basic_info) · [跨卷实测](#本地普通文件跨卷保存与恢复)

实际 Windows 和 SQLite 对照中，旧路径读取已有 40 MiB 时，独立 SQLite 写事务无法取得。新路径写完首个 1 MiB 后暂停，同算例其他路径和同账户另一算例均保存 200。大文件保存 200，历史前像大小和 SHA-256 与原件一致。另一请求抢先写入同一目标时，旧请求返回 409。直接改动文件并恢复原修改时间后，过期写入也返回 409，保留外部内容。[旧路径](../../tmp/live-file-write-lock-20261120.json) · [并发与冲突](../../tmp/local-file-write-staging-20261120.json)

真实 API 在写入私有暂存、尚未发布日志时退出。冷启动读回原 SHA-256，重试保存 200，并回收退出进程的暂存和额度。安装后而 SQL 提交前退出的 40 MiB 事务也通过冷恢复。覆盖 SQLite 和隔离 PostgreSQL 的普通上传、删除、条件更新与可移植文件模式，其中 PostgreSQL 测试已删除临时库。真实模型调用 0 次，模拟调用 0 次。[暂存退出](../../tmp/local-file-write-staging-20261120.json) · [提交前退出](../../tmp/local-file-write-recovery-20261120.json) · [SQLite 14 项](../../tmp/file-transactions-evidence-19mxhw0o/checks.json) · [PostgreSQL 4 项](../../tmp/file-transactions-evidence-znhlqvyt/checks.json)

升级后通过认证 SeaweedFS 的原日志兼容验收，实际 API 退出后可只读诊断 40 MiB 前像，恢复期限到期返回 504，下一次普通请求恢复原摘要。[原 S3 回归](../../tmp/s3-file-write-recovery-compat-20261120.json)

## 认证 S3 普通文件保存

普通保存、表单上传和删除复用既有的算例持久预约及逐对象持久额度。旧对象按版本读取，在节点额度内暂存。前后像写入、条件应用和校验在 SQL 写事务之外运行。提交时短暂复核权限、算例来源和修订，并使用同一文件事务见证记录结果。普通读取与后台事件可以在核验原写者退出后接续恢复。

认证 SeaweedFS 与 SQLite 的实测将同一 40 MiB 前像的首个 1 MiB 下载作为检查点。旧路径此时占用了 SQLite 写锁，新路径此时可获取独立写事务，同一账户的另一算例保存返回 200，同一算例的竞争写入返回 409。节点暂存和实际 S3 备份 PUT 分别观测到 40 MiB 预留，备份 PUT 等待发送时 SQLite 写事务也可取得。[旧路径](../../tmp/s3-ordinary-file-lock-baseline-20261120.json) · [并发与额度](../../tmp/s3-ordinary-file-write-20261120.json)

文件下载超过请求期限后，等待实际 I/O 排空再返回 504。原对象保留，带旧摘要重试返回 200。三次实际进程退出分别覆盖暂存中、应用后而 SQL 提交前、提交后清理前，冷 API 读回对应的旧版或新版。恢复后活动预约归零。普通上传和删除、无效摘要、过期摘要、过期修订、要求文件不存在等请求也通过。[退出和超时](../../tmp/s3-ordinary-file-write-20261120.json)

后台索引遇到同一份冷日志时释放事件投递租约，再独立恢复。实际恢复进程再次退出后索引成功续接，原事件只产生一个索引回执。[后台恢复复验](../../tmp/outbox-local-journal-s3-rr25ltn9/outbox-cold-journal.json)

只读诊断在原写者已退出时可查看待决日志。恢复超时后继续展示原有的 `recovery_required` 状态和已排空预约，下一次读取可续接同一事务。隔离 PostgreSQL 搭配认证 SeaweedFS 的正常上传和删除、提交前及提交后退出共 3 项通过，临时数据库已删除。本地 SQLite 文件事务兼容 3 项通过。[诊断兼容](../../tmp/s3-ordinary-file-diagnostics-20261120.json) · [PostgreSQL 3 项](../../tmp/s3-file-transactions-evidence-n9my0ig0/checks.json) · [本地 3 项](../../tmp/file-transactions-evidence-mh96ohip/checks.json)

## caseDict 普通编辑

全文、块、caseMeta、扩展新增和删除、错误新增和更新共七个入口使用同一提交流程。准备阶段读取原内容和两份历史，校验语法、阶段和摘要条件，生成正文、时间线、provenance 三份文件。本地同卷使用节点额度预先暂存，S3 使用持久写入预约逐对象传输。提交时复核原修订和权限，文件见证与数据库镜像同一次 SQL 提交。caseMeta 无改动时保留原修订。

实际文件系统和认证 SeaweedFS 各用 40 MiB 时间线观察 SQLite 写锁。原入口在暂停读取时持有写锁，新入口在实际首个 64 KiB 已读后仍允许另一算例保存 200。提交后保留原时间线 SHA-256，新增事件修订与数据库相同，provenance 新记录指向同一份正文。双后端七个入口、无改动及过期修订检查均通过。历史仍按原文件格式组装为内存字节，准备阶段使用有界 I/O 名额和请求期限。[旧本地](../../tmp/casedict-write-baseline-fs-20261120.json) · [旧 S3](../../tmp/casedict-write-baseline-s3-20261120.json) · [新本地](../../tmp/casedict-write-staged-fs-20261120.json) · [新 S3](../../tmp/casedict-write-staged-s3-20261120.json)

本地 SQLite 通过 7 项退出与异常检查。覆盖正文安装后、时间线安装后、提交前与提交后退出，以及历史竞争写入和损坏保留。认证 SeaweedFS 与隔离 PostgreSQL 在写到第二份文件后、提交后两处实际退出均按见证恢复，临时数据库已删除。[本地 SQLite](../../tmp/casedict-transactions-evidence-lnvqma5n/checks.json) · [S3 与 PostgreSQL](../../tmp/s3-file-transactions-evidence-xs0nf95w/checks.json)

本地文件系统搭配隔离 PostgreSQL 的提交后退出与历史竞争两项复验通过，临时库已删除。[本地 PostgreSQL](../../tmp/casedict-transactions-evidence-_bd8e1b8/checks.json)

另一隔离 PostgreSQL/S3 算例在正文与两份历史已写回、SQL 提交前终止 API。冷启动恢复三份原始 SHA-256 与原数据库镜像，随后同条件重试提交成功。回读通过 DeepSeek 官方服务的 `deepseek-flash` 完成，两次真实请求均返回 HTTP 200，模拟调用 0 次。[三文件恢复](../../tmp/live-cli-startup-et6bhp93/dictionary-crash.json) · [DeepSeek 请求证据](../../tmp/live-cli-startup-et6bhp93/live-result.json)

## caseDict 工作流推进

直接推进和带步骤的阶段确认共用字典暂存。读正文、时间线及 provenance，生成后像，并按本地同卷或认证 S3 路径准备文件。最后复核授权、来源、修订及阶段证据。在同一 SQL 提交中写入文件见证、字典镜像和受确认的工作流步骤。S3 的条件写入在 SQL 提交前依预约执行，步骤冲突或证据失效后由原见证恢复。旧几何证据使用暂存时确定的原字典摘要。[实现](../server/app/services/engineering_stage.py)

文件系统和认证 SeaweedFS 各验证直接 A→V 与真实 CadQuery 成果的 A→B 工作流确认。四次测试均在 40 MiB 时间线的首个 64 KiB 完成后暂停。此时另一 SQLite 写事务可以取得锁，同账户另一算例的实际保存和回读也成功。放行后，每次只追加一条推进事件，原历史前缀字节完全一致，provenance 摘要与新字典及 SQL 修订一致。缺少几何证据返回 409，过期步骤确认及过期修订拒绝。此前两种后端均会在相同历史读取处阻塞 SQLite 写入。[原双后端基线](../../tmp/casedict-workflow-lock-baseline-20261120.json) · [文件系统验收](../../tmp/casedict-stage-capacity-fs-20261120.json) · [认证 SeaweedFS 验收](../../tmp/casedict-stage-capacity-s3-20261120.json)

文件系统和认证 SeaweedFS 分别搭配 SQLite 与隔离 PostgreSQL。八次实际 API 退出覆盖文件安装后、SQL 提交前及提交后丢失回复。四种组合均按提交见证恢复对应的文件与镜像。PostgreSQL 临时库已回收。真实模型 0 次，模拟 0 次。[本地 SQLite](../../tmp/casedict-transactions-evidence-r_g5jzl1/checks.json) · [本地 PostgreSQL](../../tmp/casedict-transactions-evidence-bg74xr_b/checks.json) · [S3 SQLite](../../tmp/s3-file-transactions-evidence-20fwqnnt/checks.json) · [S3 PostgreSQL](../../tmp/s3-file-transactions-evidence-2_0vhal_/checks.json)

文件系统和认证 SeaweedFS 的完整工程链均经过 B、C、D、E 四次显式确认。两组共核验十二个实际作业的退出证明及发布状态，并读回包含 `mean.T@1` 的 HTML 报告。网格确认先核验发布和同步状态，再复核字典中的质量策略。[本地完整链路](../../tmp/casedict-stage-full-fs-20261120.json) · [S3 完整链路](../../tmp/casedict-stage-full-s3-20261120.json) · [逐作业终态](../../tmp/casedict-stage-terminal-audit-20261120.json)

认证 S3 的 E 阶段曾在 SQL 已提交、清理仍进行时收到 504。修复后在实际 SQL 提交完成时刻注入等待，跨过原请求期限，接口仍返回已提交修订并派发报告，所有预约最终结清。清理未结清时，接口携带 `committed: true` 和待清理状态供客户端刷新。[提交后跨期限实测](../../tmp/casedict-stage-full-s3-20261120.json)

使用真实 CadQuery 几何验证产物导入旧格式记录。在认证 SeaweedFS 上按原字典摘要进行了 B 阶段确认，接口提交新状态和字典修订。[旧几何证据复验](../../tmp/casedict-legacy-geometry-advance-s3-20261120.json)

## caseDict 失败回退

作业收尾和独立补偿在失败终态及回退意图提交后，使用相同的锁外历史读取与文件暂存。A→A 只追加失败时间线，V→C 同步提交字典、时间线、来源和修订镜像。提交时重新核对原作业、停止与排空证明、算例修订、存储来源及更晚的正式产物。文件日志见证与唯一的回退回执在同一 SQL 事务提交。重复处理返回已绑定回执，保持较新的确认及文件内容。[回退事务](workflow-failure-recovery.md#2026-11-20-锁外准备复验)

改动前，文件系统与认证 SeaweedFS 的 A→A、V→C 四次实际子进程退出都在回退读取 40 MiB 时间线时阻塞另一 SQLite 写事务。改动后，读到首个 64 KiB 即暂停，四次均允许另一 SQLite 写事务取得锁，同账户另一算例也能保存并读回。放行后原历史前缀、追加事件、字典与来源 SHA-256 及回执重放均通过。两份本地竞争算例证明，直接改动字典阶段或在历史准备后改动字典，都会保留文件并拒绝旧决定。还原原字节后回退成功。七种作业类型在 A 与 V 的 14 次实际退出亦通过阶段策略及重放检查。[失败回退基线](../../tmp/casedict-fallback-lock-baseline-20261120.json) · [双后端并发保存与文件核对](../../tmp/casedict-fallback-capacity-final-20261120.json) · [字典竞争和阶段策略](../../tmp/fallback-authority-8s1v8hgw/failure-fallback-authority.json)

本地文件系统与 SQLite、认证 SeaweedFS 与 PostgreSQL 分别通过 CadQuery 实际失败后的三处 API 退出和恢复。总共六次退出覆盖文件安装前、文件安装后且 SQL 提交前、SQL 提交后。每次只执行一次原失败作业，恢复后仅追加一条失败事件，后续确认 B 保留。已有本地阶段确认容量回归也通过。[退出恢复及实际模型回读](workflow-failure-recovery.md#2026-11-20-锁外准备复验) · [原阶段确认兼容](../../tmp/casedict-stage-fallback-compat-fs-20261120.json)

## Agent 字典提案

手动批准与全权限自动执行共用字典暂存流程。准备阶段在写事务外读取字典、时间线和来源记录。提交阶段重新核对用户的写入权限、会话租约、工具参数、算例来源、修订及文件前像。字典镜像、文件提交见证、工具状态和唯一的成功结果在同一 SQL 事务提交。S3 使用已有算例预约及逐次物理写入额度，同账户的其他算例可在历史读取期间保存。

真实 DeepSeek 选择工具后，手动批准、自动替换和整文件导入通过。文件系统和认证 SeaweedFS 均在首个 64 KiB 暂停实际读取 40 MiB 时间线，另一 SQLite 写事务取得锁，同账户另一算例保存并读回。原历史前缀、单条新增事件、字典修订与来源摘要一致。两种存储都能拒绝准备期间发生的外部编辑，保留文件和原修订。重复审批返回 409，无额外模型调用。认证 S3 的普通文件批准、额度拒绝和自动执行一并复验通过。[本地手动批准与竞争编辑](../../tmp/live-deepseek-approval-8gus98ff/live-result.json) · [认证 S3 手动批准与竞争编辑](../../tmp/live-deepseek-approval-_gf_ht92/live-result.json) · [认证 S3 自动执行](../../tmp/live-deepseek-approval-_re5fu46/live-result.json) · [本地整文件导入](../../tmp/live-deepseek-approval-hzzp0k_v/live-result.json) · [普通文件兼容](../../tmp/live-deepseek-approval-sb4cp_i5/live-result.json)

本地文件系统与 SQLite、认证 SeaweedFS 与 PostgreSQL 分别通过 SQL 提交前和提交后真实 API 退出。新 API 在启动时先结清已退出进程的文件日志，再恢复会话。提交前退出恢复原三份文件字节和原修订，工具留下唯一中断结果。提交后退出保留新字节、修订和唯一成功结果。两种情况均从正常公开入口完成读回，恢复过程无需模型推理或重新批准。远端首轮在文件预约处返回 `file_sync_busy`，增加启动清理顺序后复验通过。[本地两次退出](../../tmp/live-agent-dictionary-recovery-h9ixvgz0/live-result.json) · [认证 S3 两次退出](../../tmp/live-agent-dictionary-recovery-obdraow7/live-result.json) · [远端首轮](../../tmp/live-agent-dictionary-recovery-8uzl42xr/live-result.json)

本轮累计真实 DeepSeek 33 次，均为 HTTP 200，实际模型 `deepseek-flash`，上报 170,628 tokens，模拟调用 0 次。统计包含容量、工具冲突、普通文件兼容及进程退出场景，以及首轮拒绝空白导入、重复夹具名和 S3 启动恢复故障。重算及旧计划迁移的内部回退、普通文件提案长传输、跨卷与原系统组合继续按原范围验收。[模型拒绝首轮](../../tmp/live-deepseek-approval-3idpd1bc/live-result.json) · [重复夹具名首轮](../../tmp/live-deepseek-approval-qsayf82v/live-result.json)

复用的文件恢复入口通过认证 S3 后台事件兼容验收。实际冷启动后暂停读取 40 MiB 前像，同一账户的另一算例仍可保存并投递，原事件恢复后只有一条索引回执。[后台冷恢复](../../tmp/outbox-local-journal-s3-r8xvkgg4/outbox-cold-journal.json)

### 修改前的写锁证据

真实 DeepSeek 选择字典文本替换工具。文件系统和认证 SeaweedFS 的实际批准均在 SQLite 写事务内读取 40 MiB 历史，独立写事务无法取得锁。放行后按原审批流程提交字典及工具结果，重复批准返回 409，无额外模型调用。认证 SeaweedFS 首轮因隔离对象存储卷数不足在写入时失败，保留实际工具错误及原字典，扩大临时卷数后复跑通过。[本地审批基线](../../tmp/live-deepseek-approval-8e22_b7a/live-result.json) · [认证 S3 审批基线](../../tmp/live-deepseek-approval-xt0t0d6m/live-result.json) · [认证 S3 首轮](../../tmp/live-deepseek-approval-kzcmhxlz/live-result.json)

失败回退及审批基线阶段共调用真实 DeepSeek 9 次，均为 HTTP 200，模型 `deepseek-flash`，上报 42,134 tokens，模拟调用 0 次。4 次用于实际退出与恢复的证据回读，5 次用于真实工具选择和审批后的继续回合，包含认证 S3 首轮发出的 1 次请求。[本地退出回读](../../tmp/live-cli-startup-k8lprbow/live-result.json) · [认证 S3 退出回读](../../tmp/live-cli-startup-chyjnafk/live-result.json) · [本地审批](../../tmp/live-deepseek-approval-8e22_b7a/live-result.json) · [认证 S3 审批](../../tmp/live-deepseek-approval-xt0t0d6m/live-result.json) · [认证 S3 首轮](../../tmp/live-deepseek-approval-kzcmhxlz/live-result.json)

## 重算与旧计划迁移

重算和旧 v1 计划迁移共用锁外字典暂存。确认回到 A 后，字典镜像、文件见证、新流程与步骤、回退来源及创建事件在同一事务提交。最终提交重新检查源流程、活动作业、停止及排空证明，以及字典版本和依赖观察。S3 只允许本次预约的启动事件和绑定的文件见证消耗序号，其他新增事件仍使旧观察失效。同键请求在现有存储期限内等待或读取已提交的新流程，只有创建该流程的请求负责首次派发。

本地文件系统及认证 SeaweedFS 搭配 SQLite 的重算和迁移均在读到 40 MiB 历史首个 64 KiB 后暂停。四次实测都能取得独立 SQLite 写事务，并完成同账户另一算例保存。同一键的第二个 HTTP 请求在暂停期间抵达，两个响应返回同一流程，每组只增加一次修订。原历史前缀、来源摘要和旧流程均通过核对。S3 首轮的同键等待先于原请求结束，改为使用算例存储的现有期限后，两条路径复验通过。[本地 40 MiB 并发](../../tmp/workflow-reset-capacity-final-fs-20261120.json) · [认证 S3 40 MiB 并发](../../tmp/workflow-reset-capacity-auth-s3-20261120.json) · [S3 首轮](../../tmp/workflow-reset-capacity-final-s3-20261120.json)

本地重算和认证 SeaweedFS 迁移分别通过文件安装之后、SQL 提交前后两次实际 API 退出。提交前冷启动恢复原三份文件和旧修订，随后可重新确认并创建流程。提交后保留新三份文件和已提交的新流程，同键重新查询返回原流程。共四次实际退出，恢复无模型调用。[本地重算退出](../../tmp/workflow-reset-exit-fs-20261120.json) · [认证 S3 迁移退出](../../tmp/workflow-reset-exit-s3-20261120.json)

PostgreSQL 与 SeaweedFS 的 14 项重算回归通过，运行实际 CadQuery、OpenFOAM 网格生成与检查，并在新 API 进程读回数据库和对象存储。旧计划迁移在 PostgreSQL 搭配文件系统及认证 SeaweedFS 上复验。旧固定命令各执行一次并实际退出，迁移后以当前几何生成正确 STEP，按原流程确认 B。真实 DeepSeek 回读 4 次，均为 HTTP 200，模拟 0 次。S3 首轮在旧作业文件日志结清前读取返回 409，验收等待读接口恢复后复跑通过。[重算与冷启动](../../tmp/workflow-recompute-pg-s3-06agik10/result.json) · [本地迁移与模型](../../tmp/live-cli-startup-9j4vu9g_/live-result.json) · [认证 S3 迁移与模型](../../tmp/live-cli-startup-d2cgbdkg/live-result.json)

## Agent 普通文件提案

### 2026-11-21 提案基线读取与原子提交复验

FF-F31 已验收。提案生成通过有界算例读取池选定版本，每块最多读取 64 KiB。正文摘要在算例写事务之外计算，发布审批前复核权限和存储水位。取消和超时等待当前读取块收尾，再落库为取消或错误工具结果。正常提案携带完整 SHA-256，空文件与路径不存在分别绑定空内容摘要和显式 `null`。审批继续用所绑定版本核对原子写入。普通编辑器的条件保存及 caseDict 专用写入已有直接浏览器证据。[编辑器和 caseDict](FF-A11-editor-conflict-acceptance.md)

- SQLite 文件系统与 PostgreSQL 认证 SeaweedFS 读取 1,071,017 B 旧正文。首块暂停时独立连接均可取得同案数据库写锁。本地另一文件可保存，认证 S3 的同一目标更新可保存。基线复读后批准成功，重复批准返回 409。认证 S3 的首次后续取消用例遇到 DeepSeek 连接错误；单独复验通过。[本地三项](../../tmp/live-proposal-baseline-0d31u2uq/live-result.json) · [认证 S3 新基线及连接错误](../../tmp/live-proposal-baseline-0er_r4za/live-result.json) · [认证 S3 取消和超时](../../tmp/live-proposal-baseline-rvr1c3xb/live-result.json)
- SQLite 和认证 S3 的取消、超时均实际停在第一块。排空后无待审批记录，文件原样保留。超时生成 `proposal_baseline_timeout`，模型收到工具失败结果后完成续答。两种存储另做空文件与不存在路径的真实哈希核对。[纯存储边界](../../tmp/proposal-hash-boundaries-k2twc68q/result.json)
- Edge 触发真实审批，外部编辑保留，批准请求只发送一次，错误工具卡刷新后仍显示。文件监听记录了实际外部修改，服务端没有产生批准写入的事务见证。认证 S3 还通过替换冲突、同名文件出现及 caseDict 修改冲突。旧提案缺基线的拒绝和全权限自动写入另有实际复验。[Edge 记录](../../tmp/live-deepseek-approval-b5ln3qjz/browser-conflict.json) · [Edge 对应回合](../../tmp/live-deepseek-approval-b5ln3qjz/live-result.json) · [认证 S3 冲突](../../tmp/live-deepseek-approval-1v1j7ny4/live-result.json) · [旧提案及自动写入](../../tmp/live-deepseek-approval-mq_alr6p/live-result.json)
- 本地与认证 S3 在文件写入后分别于 SQL 提交前、提交后实际终止 API。冷启动依据提交见证回滚或保留内容，历史持久保留唯一中断或成功结果，重复审批返回 409。普通文件操作的 caseDict 摘要及修订保持原样。[本地退出](../../tmp/live-agent-file-recovery-1eue7grc/live-result.json) · [认证 S3 退出](../../tmp/live-agent-file-recovery-gudp1the/live-result.json)

真实 DeepSeek `deepseek-flash` 返回 30 次 HTTP 200，其中整组通过的运行含 24 次。上报 151,913 tokens，模拟 0 次。首轮浏览器补用本机已有 Playwright，复验将文件监听通知与已提交写入分开核对。一次模型首轮请求未选用预期工具，保留原结果。五个隔离 PostgreSQL 数据库均已移除。[本阶段汇总](../../tmp/FF-F31-file-baseline-20261121/validation.json) · [专项入口](../server/live_llm_proposal_baseline_acceptance.py)

### 既有审批暂存和大文件验收

普通文件覆写与替换走编辑器已有的暂存、写入额度和恢复日志。提案按实际存储后端的原文件摘要绑定，替换在写事务外读取固定版本、校验摘要并生成新内容，落库时重新核对用户权限、会话归属、工具参数与文件前像。文件见证与唯一成功工具结果同事务提交。S3 逐次物理写入使用绑定到算例的持久额度，手动批准和自动执行均在后续模型调用前结清额度。替换按现有事务大小限制在内存组装。[跨卷安装](#本地普通文件跨卷保存与恢复)

使用真实 DeepSeek 选工具。在文件系统和开启版本管理的认证 SeaweedFS 上分别验证 40 MiB 文件的手动文本替换、自动文本替换和手动整文件覆写。实际替换读到首个 64 KiB、整文件覆写复制首个 1 MiB 时暂停。六次暂停期间独立 SQLite 写事务均可取得，同账户的另一算例可以保存并读回。最终 SHA-256 与预期相同，普通文件提案保持 caseDict 修订，重复批准返回 409。S3 每次提案操作与其物理写入额度均结清。[本地三项](../../tmp/live-deepseek-approval-iyhie805/live-result.json) · [认证 S3 三项](../../tmp/live-deepseek-approval-2hzxa0fx/live-result.json)

本地 SQLite 与认证 SeaweedFS PostgreSQL 分别在文件安装后且 SQL 提交前、SQL 提交后实际结束 API。新 API 冷启动按见证恢复原或新文件，并保留唯一对应的工具中断或成功结果。另有真实模型兼容回归覆盖外部修改、旧提案无摘要、零额度时保留待批准和解除限制后继续。后续恢复及重复批准均无模型调用。本阶段累计真实 DeepSeek 28 次，均为 HTTP 200，实际模型 `deepseek-flash`，上报 139,160 tokens，模拟 0 次。[本地退出](../../tmp/live-agent-file-recovery-5iv6zd3c/live-result.json) · [认证 S3 退出](../../tmp/live-agent-file-recovery-wg2rsv7j/live-result.json) · [本地兼容](../../tmp/live-deepseek-approval-2ry50k7d/live-result.json) · [PostgreSQL 和 S3 兼容](../../tmp/live-deepseek-approval-ijglv3_o/live-result.json)

普通文件编辑器共用路径通过本地日志只读诊断、自动恢复以及认证 S3 40 MiB 保存、超时排空和退出回归。[本地文件公开接口](../../tmp/ordinary-agent-public-edit-fs-20261120.json) · [认证 S3 文件公开接口](../../tmp/ordinary-agent-public-edit-s3-20261120.json)

## 只读诊断

`GET /file-transactions` 先检查算例访问权，随后释放 SQL 会话事务，再校验待决日志、保留副本和当前项目内容。可选的准备记录分页也在事务之外读取。诊断最后短暂取得算例锁，复核用户权限、来源绑定、活动预约和数据库提交见证。锁释放后检查日志指针和当前对象身份。诊断不应用、移动或删除任何文件，也不清理私有暂存。

SQLite 搭配文件系统及开启版本管理的认证 SeaweedFS，各在真实 API 进程退出后诊断一份含 40 MiB 前像的日志。暂停实际正文读取时，独立 SQLite 写事务可取得，同账户另一算例的保存返回 200。两次诊断返回同一待决事务，提交见证仍为空，保留文件和准备记录。另用非特权 WSL 产生的目录快照验证，目录日志的诊断也能在不占用 SQLite 写锁的情况下核对 40 MiB 前像。[文件与 S3 诊断](../../tmp/file-diagnostics-final-20261120.json) · [本地目录诊断](../../tmp/sandbox-recovery-ui-retirement-jt4zflc3/local-tree-proof.json)

## 自动恢复

读取文件、下载、文件树和常用编辑入口在进入业务协调前检查未结日志。认证 S3 的旧文件日志由现有持久预约保护，前后像读取、条件回滚在短 SQL 控制事务之外完成。实际冷 API 普通文件版本读取成功回滚未提交的 40 MiB 文件，相同账户另一算例可以并行保存，读回长度和 SHA-256 一致。本地目录日志使用已有的锁外校验和同卷交换，在提交后结束 API 的实际场景中由普通版本读取自动完成结清。[S3 自动恢复](../../tmp/file-diagnostics-final-20261120.json) · [本地 WSL 目录恢复](../../tmp/sandbox-recovery-ui-retirement-jt4zflc3/local-tree-proof.json)

隔离环境将自动恢复期限设为 14 秒，并暂停读取真实 S3 前像。首次试验暴露期限异常被误分为日志冲突。修正后，到期等待正文排空再返回 504，日志和已排空预约保留。下一次普通版本读取接续同一日志，恢复原 40 MiB，SHA-256 一致，最终诊断回到空闲。本地普通文件兼容回归通过六项，含提交前后退出、外部编辑、损坏副本和只读准备记录分页。[S3 超时复验](../../tmp/file-auto-recovery-verified-20261120.json) · [文件系统兼容](../../tmp/file-transactions-evidence-ohybkiq9/checks.json)

## 普通本地文件的锁外回滚

普通文件请求和显式恢复入口复用同一个恢复流程，并共用每个 API 进程的 4 个复制名额。先在 SQL 事务外核验日志和前后像，再按节点额度将要恢复的前像复制到目标卷的进程私有目录。最终协调锁用于复核 SQL 提交见证、来源、文件身份和账户额度，安装已校验文件，并结清原日志。

实际在 Windows、SQLite 和本地文件系统上先让 API 在写入后、SQL 提交前退出，留下真实的 40 MiB 前像。冷启动后暂停真实读盘和复制，复制已写入 1 MiB 时，SQLite 写事务可取得，同账户另一算例保存 200。14 秒期限到期后等复制排空再返回 504，清理暂存并保留待决日志。在复制期间直接编辑目标后，旧恢复返回 409 并保留该编辑。通过显式入口再次恢复时，让 API 在替换完成、结清日志前实际退出，下一次普通版本读取自动恢复原 SHA-256，最后没有活动暂存目录或磁盘额度。[40 MiB 本地冷恢复验收](../../tmp/local-file-recovery-verified-20261120.json)

SQLite 原文件事务 9 项、隔离 PostgreSQL 原文件事务 4 项兼容回归通过，覆盖提交前后退出、外部编辑、批量恢复等场景，PostgreSQL 临时库已删除。认证 SeaweedFS 的 40 MiB 只读诊断、超时排空和自动接续再次通过。真实模型调用 0 次，模拟调用 0 次。[SQLite](../../tmp/file-transactions-evidence-6zat13u7/checks.json) · [PostgreSQL](../../tmp/file-transactions-evidence-a72cmbm8/checks.json) · [认证 S3](../../tmp/s3-file-recovery-compat-20261120.json)

## 本地普通文件跨卷保存与恢复

日志在待决目录所在卷准备，替换文件在各自目标卷准备。保存和冷回滚的正文复制在 SQL 写事务之外完成，提交锁内以同卷重命名安装。目标挂载的 `.vibeflow-file-staging` 计入存储盘点并独立检查目标卷可用空间。文件浏览、索引和尝试封存跳过私有暂存，只有确认所属 API 进程退出后才回收其残留目录。[暂存与额度](local-file-storage-admission.md#跨卷暂存的空间准入)

WSL 上的隔离 Linux API 使用原生 ext4 存储日志、独立 tmpfs 挂载存储目标文件。替换 40 MiB 文件时，在读取前像首个 1 MiB 后暂停。旧实现阻挡 SQLite 写事务，新实现允许取得写事务并完成同账户另一算例的真实保存。新内容和历史前像均与预期 SHA-256 一致。公开文件树和下载入口检查了私有暂存路径。[跨卷旧路径](../../tmp/cross-volume-file-baseline-20261120.json) · [跨卷新实现](../../tmp/cross-volume-file-20261120.json)

API 在替换后、SQL 提交前实际退出，冷启动后从日志向目标卷复制旧文件。暂停在首个 1 MiB 时，另一算例仍能保存，目标卷记录 41,947,136 字节预留。释放后恢复 40 MiB 原文件，核对 SHA-256 和 `rolled_back` 回执。API 在 SQL 提交后、日志结清前实际退出时，冷启动保留新内容并生成 `committed` 回执。两次恢复均清理了目标卷的进程暂存，最终节点预留归零。Windows 上的同卷只读诊断、复制超时、竞争修改、第二次 API 退出与自动接续也通过。本段文件系统验收真实模型调用 0 次，模拟 0 次。[Linux 冷恢复](../../tmp/cross-volume-file-20261120.json) · [Windows 兼容](../../tmp/file-transaction-diagnostics-cross-volume-nt-20261120.json)

修改后的共享写入入口在 Windows 上另用真实 `deepseek-flash` 复验两个 40 MiB 的 Agent 手动替换和覆写。4 次推理均为 HTTP 200，合计 19,773 tokens，模拟 0 次。两条流程完成批准、并发另案保存、摘要对照、唯一工具结果与重复批准拒绝。[真实模型复验](../../tmp/live-deepseek-approval-mqyi4dvj/live-result.json)

WSL 默认 DrvFS 的两项失败单独保留。日志在 DrvFS 时，Linux 的不覆盖目录重命名返回 `EINVAL`。目标在默认 DrvFS 时，设置 POSIX 文件模式返回 `EPERM`。通过的跨卷证据适用于 ext4 和 tmpfs 组合。[日志目标失败](../../tmp/cross-volume-file-byxlt68n/api.log) · [权限失败](../../tmp/cross-volume-file-8dycqr8l/api.log)

## 后台事件遇到未结日志

派生索引读取在算例锁内只识别待决日志，文件校验和回滚交给原有的锁外流程。事件先退回持久待投递状态，清除投递租约。恢复沿用独立截止时间，每个投递器至多运行一个文件恢复任务，其他事件仍可正常投递。源算例或日志改变时，下次重试重新核验。S3 仍使用持久存储预约和进程停止证明。

SQLite 搭配本地文件系统和认证 SeaweedFS 均通过实际 API 退出后的冷恢复。暂停读取 40 MiB 前像超过原 10 秒投递期限时，原事件保持待投递、无租约，同一账户的另一算例仍可保存并完成索引投递。释放读取后原事件重投成功，索引回执只有一条，恢复文件 SHA-256 与原件一致。真实模型调用 0 次，模拟调用 0 次。[本地文件系统](../../tmp/outbox-local-journal-fs-262ir0aj/outbox-cold-journal.json) · [认证 S3](../../tmp/outbox-local-journal-s3-qc3x4a9v/outbox-cold-journal.json)

另一次认证 S3 验收在恢复线程仍停在正文读取时终止 API。重启后原事件按已保存的重试时间重新取用，核验原恢复进程已退出，再完成同一文件日志。第三次投递留下一个索引回执，文件 SHA-256 一致。分离恢复代码后，非特权 WSL 的本地目录回滚、过期副本拒绝及提交后结清也重新通过。[S3 二次退出](../../tmp/outbox-local-journal-s3-pf97xf92/outbox-cold-journal.json) · [WSL 目录兼容](../../tmp/sandbox-recovery-ui-retirement-edy4oo86/local-tree-proof.json)

索引旧回归改用会正式发布产物的类型化作业，并通过只读投影检查已有索引，避免公开搜索的自动对账影响断言。SQLite、PostgreSQL 各通过五项实际回归，覆盖投递前退出、索引提交后退出、故障回滚、重投幂等及连续作业刷新。独立索引事务边界另通过五项，临时 PostgreSQL 数据库已删除。[兼容回归](../../tmp/event-consumer-evidence-6ovgic0l/result.json)
