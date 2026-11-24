# 提交事务的存储额度与原生执行交接

## 2026-11-21 CalculiX 与 SU2 的公开求解提交

两个公开入口共用封存流程，在数据库写事务外生成求解器配置、读取输入和历史发布、校验输入摘要及准备作业快照。文件系统按节点和目标卷额度暂存，认证 S3 使用持久预约。提交验证已封存的依赖集合以及之前读取的文件和发布版本。一开始缺失的可选运行配置也在版本检查范围内。读取旧 S3 发布时以远端对象校验取代本地成果缓存恢复。

旧实现读取 CalculiX 的 2,162,708 B INCLUDE 文件和 SU2 的 485,272 B 网格时均持有 SQLite 写锁。SQLite、PostgreSQL 分别搭配文件系统和认证 SeaweedFS 的四组修复验收中，在相同观测点可取得独立数据库写锁或算例锁，同账户另一算例实际写入成功。原生 CalculiX 发布含非零位移的 FRD，SU2 发布含密度场的 VTU。四组在暂存后新增 `runtime.cfg` 均返回 409，同一个请求键随后封存真实配置并完成 SU2 求解。CadQuery 认证 S3 共用预约、并发幂等键及实际成果发布回归通过。

四组修复验收、旧路径基线及 CadQuery 回归共记录真实 DeepSeek 12 次 HTTP 200，模拟 0 次。六个 API、三个认证对象存储实例和两个临时 PostgreSQL 库均已清理。[实测索引和源码摘要](../../tmp/native-solver-submission-capacity-validation-20261121.json)

## 2026-11-21 公开 CadQuery 几何提交

预览、导出以及保存 `geometry.py` 后的自动预览，在打开 SQL 写事务前准备输入封存。文件系统复用目标卷预留和原子目录安装，S3 复用持久算例预约与已版本化的封存对象。几何来源摘要使用已封存的文件记录，caseDict 正文复核原始 SHA-256 后生成相同格式的工程输入指纹。写事务中复核算例权限、已封存版本和上一代发布状态，原子登记作业、封存见证和发布意图。保存触发的预览还比较本次保存的脚本摘要。

旧实现对 12 MiB 几何输入完成实际摘要读取时仍持有 SQLite 写锁。新实现的 SQLite、PostgreSQL 各搭配文件系统和认证 SeaweedFS，四组都能在相同观测点取得独立写事务或算例锁，并完成同账户另一算例保存。四组实际 CadQuery 预览、STEP/STL 导出和保存触发的预览均发布成功。暂存后等长替换输入返回 409，同键重试使用新输入。PostgreSQL 文件系统和 SQLite S3 额外验证并发同键归并为一个作业，改用不同容差的请求返回 409。既有 CadQuery 原失败作业重试和按当前输入重跑均通过。

本轮含旧路径基线、四组修复验收和真实作业重试回归。实际 DeepSeek 12 次，均 HTTP 200，模拟 0 次。六个隔离 API、两个认证 S3 进程和两个临时 PostgreSQL 库均已清理。[源码绑定与实测索引](../../tmp/geometry-submission-capacity-validation-20261121.json)

## 2026-11-21 文件系统重试与续算的专业来源读取

直接重试、工作流重试和检查点续算在 SQL 写事务外读取和核验专业输入及发布依赖。提交时复核读取过的文件身份、Windows 文件变化时间、目录条目、缺失路径和发布数据库记录。注册处理器的读取范围按原输入规则确定；会动态发现依赖的处理器用完整公开算例目录的元数据。发布文件和发布清单按实际选取路径记录。当前 30 个注册处理器都有对应的文件检查范围，实际回归范围见后面的验收记录。 [文件版本记录](../server/app/services/local_source_reads.py) · [作业重试](../server/app/services/job_repeat.py) · [工作流恢复](../server/app/services/workflow_resume.py)

| 实际 CadQuery 工作流在读取 12,582,934 B 来源文件之后暂停 | 独立 SQLite 写锁 | 同账户另案保存 |
| --- | --- | --- |
| 旧版，事务内第二次读取 | 无法取得 | 本轮未执行 |
| 新版，事务外读取 | 取得成功 | 成功 |

SQLite 的同键并发在一次存储结算忙后返回同一个新流程，只分配一个新作业。PostgreSQL 在读取后等长改写实际几何来源并保持修改时间，提交返回 `input_stale`，没有新增作业或幂等回执。恢复原件后，同键提交成功，CadQuery 正式发布，原流程仍为取消状态。 [旧版](../../tmp/live-cli-startup-d4d3lyy3/workflow-retry-capacity.json) · [SQLite 新版](../../tmp/live-cli-startup-yg2w8n2l/workflow-retry-capacity.json) · [PostgreSQL 来源变化](../../tmp/live-cli-startup-7g4usecp/workflow-retry-capacity.json)

真实 OpenFOAM 工作流两次续算保留手工审批，同键回读返回原流程，最终 400 单元速度和压力相对不间断运行的最大差值均为 0。真实后处理和报告的五类运行通过，PDF/PPT 的六个实际文件作业在失败回退的时间线并发问题修复后通过。 [OpenFOAM](../../tmp/live-cli-startup-sar5w_j3/solve-resume.json) · [后处理和报告](../../tmp/live-cli-startup-rfwbw7f6/post-report-repeat.json) · [PDF 和 PPT](../../tmp/live-cli-startup-spyvooxi/document-repeat.json)

失败回退重新准备时，分别在形成草稿后和复制原时间线时追加了真实事件。已控制的旧冲突返回 `file_conflict`。复制时文件超过本次记录的长度也归为原文件变化。重试会按原请求期限重新准备，保留两条已追加事件各一次，最终发布 CadQuery 成果。五类类型化命令复跑通过，覆盖原封存、当前输入、发布和修改后拒绝。 [时间线旧冲突](../../tmp/live-cli-startup-7f0xij7q/live-result.json) · [PostgreSQL 两次时间线追加](../../tmp/live-cli-startup-0_tdhcnz/fallback-timeline-capacity.json) · [类型化命令](../../tmp/live-cli-startup-34vimzjd/typed-repeat.json)

认证 S3 的工作流对照首轮已走完实际 CadQuery 提交与发布，模型回读在连接阶段失败。复跑完成实际发布和真实 DeepSeek 回读。阶段共 20 次 `deepseek-flash` HTTP 200，模拟 0 次。隔离 API 与 SeaweedFS 已退出，临时 PostgreSQL 数据库已删除。C 盘可用空间从 23,549,464,576 B 变为 23,541,698,560 B，本段 Docker 构建缓存回收 0 B，下一次维护在本机 09:00。 [S3 首轮](../../tmp/live-cli-startup-nlxdfk8s/live-result.json) · [S3 复跑](../../tmp/live-cli-startup-ra_28ibj/workflow-retry-capacity.json) · [验收汇总](../../tmp/professional-source-capacity-validation-20261121.json)

## 2026-11-21 OpenFOAM 检查点续算输入封存

直接作业和工作流续算都先校验原尝试、原封存输入和指定时刻的实际输出，再在 SQL 写事务外准备新尝试。封存只替换续算的 `controlDict` 和检查点记录，并纳入已核验的历史场文件。提交时验证原尝试版本、输入与输出摘要、实际文件或对象版本、发布读集和新封存见证。新尝试与原请求族关联，提交成功后由原投递流程启动。 [续算封存](../server/app/services/checkpoint_seal.py) · [直接作业](../server/app/services/job_repeat.py) · [工作流入口](../server/app/services/workflow_resume.py)

| SQLite 文件系统，两次实际续算 | 暂存输入大小 | 每次新增完整副本 | 两次的独立写锁 |
| --- | --- | ---: | --- |
| 旧版，复制后仍持有 SQL 写事务 | 99,256 B 与 130,355 B | 2 份 | 均无法取得 |
| 新版，复制后等待提交 | 99,256 B 与 130,355 B | 2 份 | 均取得成功 |

400 单元实际 icoFoam 求解在写出 0.005 后取消。第一次续算写出 0.01 后再次取消，第三个同根尝试从 0.0105 开始执行，最终到达 0.5。续算和不间断求解的速度与压力场最大差值均为 0。损坏字段、网格、调用记录及不合法检查点时刻仍被拒绝。 [旧版基线](../../tmp/live-cli-startup-lc84etez/checkpoint-seal-capacity.json) · [SQLite 新版](../../tmp/live-cli-startup-r4ljremm/checkpoint-seal-capacity.json)

文件系统 PostgreSQL 从工作流入口实际执行两次检查点续算。同幂等键返回同一工作流，原流程保持取消，最后停在 D 阶段人工确认；此次验收取消等待中的流程后才发起独立数值对照。S3 PostgreSQL 首轮本机已确认两次封存写入均没有新增完整本地副本，第三次实际求解到 0.5 后，发布收尾超过本轮 120 秒墙钟预算。 [工作流 PostgreSQL](../../tmp/live-cli-startup-6d4bvknz/solve-resume.json) · [S3 首轮超时](../../tmp/live-cli-startup-o6a5kt2v/live-result.json)

文件系统暂存后等长改写真实的 0.005 速度场文件并保留修改时间，旧请求返回 `input_stale`，没有新增作业，已准备的目录完成清理。恢复原文件后，原幂等键成功提交，两个检查点连续求解完成，最终场值通过独立数值比较。 [暂存后字段变化](../../tmp/live-cli-startup-qc1wdvo1/checkpoint-seal-capacity.json)

独立读取 S3 PostgreSQL 首轮落在隔离运行目录的 0.5 结果，400 单元场值与文件系统完整新算的结果相同。该轮仍标记为发布收尾超时。S3 SQLite 的工作流首轮也在 120 秒测试预算下触发资源限制。复跑明确将隔离 S3 测试预算设为 240 秒。 [超时轮场值对照](../../tmp/live-cli-startup-o6a5kt2v/native-fields-while-pending-publication.json) · [S3 工作流首轮](../../tmp/live-cli-startup-oa1ar059/live-result.json)

认证 S3 的 PostgreSQL 直接作业与 SQLite 工作流复跑均正式发布。准备完成时，新尝试没有新增完整本地副本，两个原档校验仍使用各自独立的临时读视图。SQLite 两次均可取得独立写锁。两轮保留原尝试、同键回执和原算例控制文件，S3 工作流仍等待 D 阶段人工确认，两个续算与各自不间断对照的速度和压力最大差值均为 0。 [S3 PostgreSQL 直接作业](../../tmp/live-cli-startup-gcl4xl72/checkpoint-seal-capacity.json) · [S3 SQLite 工作流](../../tmp/live-cli-startup-7lcagf3t/solve-resume.json)

真实 DeepSeek `deepseek-flash` 回读 12 次，均为 HTTP 200，模拟 0 次。隔离 API 与 SeaweedFS 已退出，临时 PostgreSQL 数据库已删除。C 盘可用空间由 23,555,604,480 B 变为 23,549,784,064 B，本阶段 Docker 构建缓存回收 0 B，下一次定时维护在本机 09:00。文件系统提交前的专业来源读回见本页后续验收。 [验收汇总](../../tmp/checkpoint-seal-capacity-validation-20261121.json)

## 2026-11-21 工作流重试的原封存输入

公开 `POST /workflow-runs/{id}:resume` 的 `retry_step` 复用原尝试，并先在写事务外准备输入。文件系统使用进程归属明确的私有目录准备 `inputs` 和 `run`。S3 在算例租约内，将原封存校验后上传到新的尝试前缀。最后复核原流程、步骤、尝试、预算、工程状态和发布读集。新流程、步骤、原尝试关联、封存见证和投递凭证仍一次提交。 [工作流恢复入口](../server/app/services/workflow_resume.py) · [重试尝试分配](../server/app/services/job_execution.py)

| 文件系统，12,582,934 B 输入完成复制时 | 新增完整本地副本 | 独立 SQLite 写锁 |
| --- | ---: | --- |
| 旧版，仍在提交事务内 | 2 份 | 无法取得 |
| 新版，尚未安装副本 | 2 份 | 取得成功 |

SQLite 暂停时另案成功保存，同键并发返回同一个新流程。PostgreSQL 在暂存后直接等长替换原封存并保留修改时间，提交返回 409，作业列表及恢复请求查询无新增结果，临时副本清理完成。恢复原件后，原幂等键成功提交，真实 CadQuery 发布了成果，原流程保持取消状态。 [旧版](../../tmp/live-cli-startup-k744tvxo/workflow-retry-capacity.json) · [SQLite](../../tmp/live-cli-startup-xc8qyp0z/workflow-retry-capacity.json) · [PostgreSQL](../../tmp/live-cli-startup-gqng1gr7/workflow-retry-capacity.json)

认证 SeaweedFS 已确认写入、数据库尚未提交时，新尝试目录中没有完整本地输入副本。同账户另案可保存，SQLite 独立写锁可取得，两个数据库的同键并发均返回一个新流程，实际 CadQuery 正式发布。首轮 500 由租约结构未携带修订字段造成。复跑改为从数据库读取该字段，核对修订与前缀之后才继续。 [S3 SQLite](../../tmp/live-cli-startup-uyqzz6gi/workflow-retry-capacity.json) · [S3 PostgreSQL](../../tmp/live-cli-startup-sctfp6i2/workflow-retry-capacity.json) · [首轮记录](../../tmp/live-cli-startup-m3kbtnj8/live-result.json)

PostgreSQL 与认证 SeaweedFS 在对象已上传、SQL 尚未提交的边界实际停止 API。重启后无新作业或幂等回执，原键提交清理未绑定对象并留下回滚日志，最终仅一个新流程发布成果。另见本页检查点续算与专业来源读取。 [真实退出恢复](../../tmp/live-cli-startup-w6ytp91c/workflow-retry-capacity.json)

直接作业的 OpenFOAM 兼容回归通过。真实 icoFoam 连续两次取消后，第三次同根尝试从 0.01 检查点续算到 0.5，与不间断运行的速度和压力场最大差值均为 0。工作流容量验收和这轮兼容回归的真实 DeepSeek `deepseek-flash` 回读合计 14 次，全部 HTTP 200，模拟 0 次。隔离 API、SeaweedFS 已结束，临时 PostgreSQL 数据库已删除。C 盘可用空间从 23,556,595,712 B 变为 23,555,702,784 B。本阶段 Docker 构建缓存回收 0 B，下一次定时维护在本机 09:00。 [OpenFOAM 兼容回归](../../tmp/live-cli-startup-criptlc5/solve-resume.json) · [验收汇总](../../tmp/workflow-retry-capacity-validation-20261121.json)

## 2026-11-21 文件系统作业按当前输入重跑

公开 `POST /jobs/{job_id}:repeat` 的文件系统重跑先在私有暂存区按当前输入准备独立的 `inputs` 和 `run` 目录。写事务复核算例修订、确认游标、原尝试与发布记录，并按文件身份及版本检查当前输入和暂存副本。检查通过后，复用封存恢复日志和无覆盖重命名，登记新尝试并写入投递请求。 [重跑入口](../server/app/services/job_repeat.py) · [文件系统封存](../server/app/services/local_rerun_seal.py)

| SQLite，两份输入目录复制完成后的观测点 | 12,582,937 B 输入的完整副本 | 独立 SQLite 写锁 |
| --- | ---: | --- |
| 旧版，复制和安装已经在提交事务内执行 | 2 份，共 25,165,874 B | 无法取得 |
| 新版，复制完成，安装尚未开始 | 2 份，共 25,165,874 B | 取得成功 |

同账户另案在暂停期间可保存，同键并发请求返回同一作业。PostgreSQL 在暂停期间同案保存后，旧确认返回 409；SQLite 中保持原大小与修改时间的直接文件替换也返回 409。两个失败请求均未留下新作业或暂存副本，重新确认后可使用原幂等键提交，新根实际运行当前 SHA-256 一次，原尝试仍可核对。 [旧版基线](../../tmp/live-cli-startup-5gz7e24k/local-rerun-capacity.json) · [SQLite 写锁和幂等](../../tmp/live-cli-startup-phhtnp9v/local-rerun-capacity.json) · [PostgreSQL 同算例编辑](../../tmp/live-cli-startup-2uxiz6xq/local-rerun-capacity.json) · [等长直接替换](../../tmp/live-cli-startup-p4ylrxma/local-rerun-capacity.json)

直接替换的复验在放行前读取了同一个确认游标。SQLite 在目录安装后、提交前实际停止 API，重启后作业列表未出现中断请求，已安装目录按恢复日志回滚，原幂等键成功执行。认证 SeaweedFS 的独立写入、同键并发回归通过。 [游标不变的等长替换](../../tmp/live-cli-startup-q65_3h4q/local-rerun-capacity.json) · [安装中断](../../tmp/live-cli-startup-3v3adeq5/local-rerun-capacity.json) · [S3 重跑回归](../../tmp/live-cli-startup-zeb59glc/job-rerun-capacity.json)

实际后处理、报告草稿、自动生成草稿及正式报告的五类重试与重跑通过，覆盖 80 单元导热数据和成果依赖。PostgreSQL 的 PDF/PPT 首轮封存重试返回 409，带异常记录的诊断复跑通过了六个实际文档作业。保留首轮记录，便于继续追查间歇失败。 [后处理和报告](../../tmp/live-cli-startup-humwz114/post-report-repeat.json) · [PDF/PPT 首轮](../../tmp/live-cli-startup-kzxrba9k/live-result.json) · [PDF/PPT 复跑](../../tmp/live-cli-startup-_d8ei9mp/document-repeat.json)

文件系统双 API 的取消、原封存重试、按明确路径重跑和恢复拒绝检查通过。PostgreSQL 另通过默认整案选取重跑。真实 DeepSeek `deepseek-flash` 回读 22 次，均为 HTTP 200，模拟 0 次。验收使用的隔离 API 已停止，临时 PostgreSQL 数据库已删除，隔离 SeaweedFS 已退出。C 盘可用空间由 23,537,258,496 B 变为 23,557,283,840 B，本阶段 Docker 构建缓存回收量为 0 B，下一次定时维护在本机 09:00。工作流重试、检查点续算和其他长文件操作仍按 RB-12、RB-14 验收。 [双 API 回归](../../tmp/live-cli-startup-9mjav2zf/job-repeat-policy.json) · [整案选择回归](../../tmp/live-cli-startup-okub9zxq/job-repeat-checks.json) · [验收汇总](../../tmp/local-rerun-capacity-validation-20261121.json)

## 2026-11-21 S3 作业按当前输入重跑

公开 `POST /jobs/{job_id}:repeat` 的 S3 `rerun` 在算例租约内读取当前输入，并在 SQL 写事务外上传新作业的封存文件。提交时复核原尝试、算例修订、确认时的事件游标和上游发布记录。提交可以跨过本次请求发出的租约开始事件。其他事件会使原确认失效。当前文件版本和封存对象版本均须与准备结果一致，新作业、封存见证和投递一并提交。 [重跑协调](../server/app/services/job_repeat.py) · [S3 封存](../server/app/services/command_input_seal.py)

| 认证 SeaweedFS 已确认上传，SQL 尚未提交 | 12,582,932 B 输入的本地完整副本 | 独立 SQLite 写锁 |
| --- | ---: | --- |
| 旧版 | 3 份，共 37,748,796 B | 无法取得 |
| 新版 | 1 份，共 12,582,932 B | 取得成功 |

新版暂停期间，同一算例的文件修改返回 `file_sync_busy`，同账户另一个算例可以保存。源文件更新后旧确认返回 `input_stale`。重新确认的新作业以新根和新 SHA-256 实际执行一次，原尝试保持原 SHA-256。并发同键的两个请求均得到 202，复用同一条作业。实际 CadQuery 验证注册处理器绑定、新旧几何版本及成果发布。 [旧版基线](../../tmp/live-cli-startup-q64a1sed/job-rerun-capacity.json) · [SQLite 写锁与幂等](../../tmp/live-cli-startup-izuf8bvz/job-rerun-capacity.json) · [PostgreSQL CadQuery](../../tmp/live-cli-startup-3f7k_26r/job-repeat-geometry.json)

PostgreSQL 的整案输入重跑，在上传暂停期间写入新的持久事件。上传返回后，旧确认得到 409，作业列表保持原样。按新游标用原幂等键再次提交，实际使用新 SHA-256，保持整案选取且仅执行一次。 [整案重跑与事件交错](../../tmp/live-cli-startup-ipu53dlj/job-rerun-capacity.json)

PPT 初次重跑在旧封存恢复处返回 409。记录表明，校验漏掉了处理器生成的 `.vibeflow/prepared` 文件，导致有效记录无法结清。恢复现在按封存清单校验这个目录内已声明、不可执行的生成配置。真实 PPT 重跑嵌入更新后的图片，旧 PPT 内容保持，生成配置的 SHA-256 已核对。同一轮实际类型化转换命令也读取了更新后的文件并正式发布。 [旧恢复报错](../../tmp/live-cli-startup-buddthte/actual-seal-recovery-blocked.json) · [恢复校验](../server/app/services/attempt_seal_journal.py) · [真实 PPT 与类型化命令](../../tmp/live-cli-startup-85z84hxd/job-rerun-registered.json)

PostgreSQL 的 PDF/PPT 故障回归复验通过。两个进程因真实输出冲突失败后，用原封存重试、用当前输入重跑，合计六项实际文档作业通过，并保留原发布。五类类型化命令的扩展回归中途在保存文件时遇到 `file_sync_busy`，该整组验收仍待补跑。 [PDF/PPT 故障回归](../../tmp/live-cli-startup-83_t87aw/document-repeat.json) · [类型化命令扩展记录](../../tmp/live-cli-startup-bqqeg9tz/live-result.json)

真实 DeepSeek `deepseek-flash` 回读共 14 次，均为 HTTP 200，模拟 0 次。隔离 API 和 SeaweedFS 已退出，三套通过验收的 PostgreSQL 临时库已删除。C 盘可用空间由 23,574,122,496 B 变为 23,570,853,888 B，本段 Docker 构建缓存回收量为 0 B，定时维护下一次在本机 09:00。文件系统重跑、工作流重试和检查点续算继续按 RB-12、RB-14 验收。 [验收汇总](../../tmp/job-rerun-capacity-validation-20261121.json)

## 2026-11-21 文件系统作业重试在写事务外复制原输入

公开 `POST /jobs/{job_id}:repeat` 的文件系统 `retry` 先读回原尝试和封存清单，释放 SQL 事务，再在进程归属明确的私有暂存区分块校验并创建这次重试的独立 `inputs`、`run` 两棵目录。暂存使用原节点空间配额和有界复制线程。提交时复核重试权限、原尝试状态、原档及两份准备目录的文件版本和库存额度，登记原封存恢复计划，并用无覆盖重命名安装完整目录。作业、输入见证和投递一并提交。 [文件系统封存](../server/app/services/local_retry_seal.py) · [重试协调](../server/app/services/job_repeat.py) · [原封存日志](../server/app/services/attempt_seal_journal.py)

| SQLite 文件系统，复制完成后的观测点 | 12,582,938 B 输入的新增完整副本 | 独立 SQLite 写锁 |
| --- | ---: | --- |
| 旧版，仍在提交事务内 | 2 份，共 25,165,876 B | 无法取得 |
| 新版，安装和提交尚未开始 | 2 份，共 25,165,876 B | 取得成功 |

新版暂停在复制完成、安装前，同账户另一个算例可以保存。替换当前算例文件并提升字典修订后，真正运行的 Windows 作业仍按原修订和原 SHA-256 执行一次。两份本地副本分别提供封存原件和可写的运行目录。 [旧版本](../../tmp/live-cli-startup-urucqo4n/local-retry-capacity.json) · [新版 SQLite](../../tmp/live-cli-startup-dkupxli3/local-retry-capacity.json)

SQLite 在私有暂存阶段实际停止 API，重新启动后按原键重试，清除已退出进程的暂存副本。PostgreSQL 在目录安装后、提交前实际停止 API，重启后无新作业，原键重试按既有恢复日志清理未提交目录并运行一次。PostgreSQL 另验证了复制完成后等长修改原文件且保留 `mtime` 的情况，提交返回 409，恢复原件后同键通过。文件系统双 API 的取消、同键并发、旧运行目录缺失和封存损坏检查通过。 [暂存退出](../../tmp/live-cli-startup-mineplz3/local-retry-capacity.json) · [安装退出](../../tmp/live-cli-startup-qg1ytpg1/local-retry-capacity.json) · [等长修改](../../tmp/live-cli-startup-cjg9kqm2/local-retry-capacity.json) · [文件系统双 API](../../tmp/live-cli-startup-bgivbtqi/job-repeat-policy.json)

SQLite 在复制完成后调低隔离账户的存储额度，提交返回 429，暂存清理完成且无新作业。恢复原额度后同键通过。真实 CadQuery 再验证原失败回退先结算及新几何发布。认证 SeaweedFS 回归继续通过旧修订、同键并发和零份新增完整本地副本。模型回读共 18 次真实 DeepSeek `deepseek-flash` HTTP 200，模拟 0 次。两套 PostgreSQL 临时库已删除，隔离 SeaweedFS 已退出。C 盘实际可用空间从 23,577,944,064 B 变为 23,574,257,664 B，阶段内 Docker 构建缓存回收 0 B。 [额度重核](../../tmp/live-cli-startup-tf8bcs4h/local-retry-capacity.json) · [CadQuery 回退顺序](../../tmp/live-cli-startup-bi6ilyxz/job-retry-fallback-race.json) · [S3 兼容](../../tmp/live-cli-startup-67obx3p5/job-retry-capacity.json) · [验收汇总](../../tmp/local-retry-capacity-validation-20261121.json)

公开文件系统重试的专业来源与发布依赖校验仍在事务中。工作流重试、检查点续算及其余长文件路径继续纳入 RB-12、RB-14。

## 2026-11-21 S3 作业重试沿用原封存输入

公开 `POST /jobs/{job_id}:repeat` 的 `retry` 操作先取得算例租约，在 SQL 写事务外从原尝试的私有对象有条件地读取文件，分块核对 SHA-256，再上传到新尝试的私有前缀。上传前登记可恢复的封存草稿。单个大文件使用既有节点空间准入和一个临时上传文件，上传返回后释放。原输入清单、字典修订、目录、生成文件和已绑定的发布来源随重试保留。 [重试协调](../server/app/services/job_repeat.py) · [封存与上传](../server/app/services/command_input_seal.py)

短事务复核当前末次尝试、停止证明、原归档和新对象的版本、上游发布记录和访问权限，再绑定重试作业、尝试谱系和投递。不同幂等键继续拒绝重试已退役的末次尝试。新任务运行目录由执行阶段从封存对象恢复。 [归档读写](../server/app/services/attempt_objects.py) · [重试作业分配](../server/app/services/job_execution.py)

| SQLite 与认证 SeaweedFS | 上传结束、SQL 提交前的重试新增本地副本 | 独立 SQLite 写锁 |
| --- | ---: | --- |
| 旧版 | 3 份，共 37,748,799 B | 无法取得 |
| 新版 | 0 份 | 取得成功 |

原算例的 12,582,933 B 输入替换后，实际 Windows 作业仍读取原封存摘要且只执行一次。同账户的另一个算例可在新版重试暂停期间保存。旧失败记录可按原 ID 查询。表中测量包含新增的完整本地副本，观测点在单个文件的上传临时缓冲已经释放之后。 [旧版对照](../../tmp/live-cli-startup-jz8vkn9f/job-retry-capacity.json) · [SQLite 与 S3](../../tmp/live-cli-startup-8whvcyvn/job-retry-capacity.json) · [专项入口](../server/live_llm_job_retry_capacity.py)

PostgreSQL 与认证 S3 在确认上传后实际停止 API。重启时旧作业列表保持原状，原幂等键回滚封存草稿、清除未绑定对象，然后成功运行一次。SQLite 同样在上传完成后用真实 S3 请求替换等长的原封存对象，事务复核返回 409，仍可在恢复原对象后按相同键完成重试。两组均保留原字典修订 0，当时当前修订已为 1。 [PostgreSQL 退出恢复](../../tmp/live-cli-startup-xwc8op9k/job-retry-capacity.json) · [S3 原对象版本复核](../../tmp/live-cli-startup-893yjoit/job-retry-capacity.json)

首次同键并发出现 `storage_quota_busy`。入口在租约准入阶段遇到原请求尚未结算的额度冲突时，会在原期限内查询提交回执。复验两次请求均返回 202 和同一个作业 ID，只有一个请求创建作业。文件系统双 API 的取消、幂等重试、当前内容重跑及封存损坏拒绝仍通过。 [首次并发](../../tmp/live-cli-startup-1tims6ih/live-result.json) · [SQLite 同键复验](../../tmp/live-cli-startup-or_vjs8g/job-retry-capacity.json) · [文件系统兼容](../../tmp/live-cli-startup-egsv24l4/job-repeat-policy.json)

CadQuery 回归发现原失败作业异步写入阶段回退记录时会占用同一算例的存储租约，阻断重试的启动或成果收尾。现在已有回退意图的原失败作业先按原流程结算，再封存和投递重试。PostgreSQL 验收将原后台回退暂停在取得租约之前，从重试请求中提交原失败记载，随后实际运行并发布新几何。后台任务恢复后沿用已登记的回执。 [启动失败](../../tmp/live-cli-startup-ma7mnsan/live-result.json) · [租约诊断](../../tmp/live-cli-startup-jdxs5nta/retry-geometry-storage-busy.jsonl) · [真实 CadQuery 交错验收](../../tmp/live-cli-startup-91jg3_bx/job-retry-fallback-race.json)

独立 CadQuery 回归再验证原文件重试、正式处理器归属、当前文件重跑以及各自发布。首次回归在重跑时因后台事件推进被拒绝。复验通过原查询入口刷新事件序号，逐项确认原尝试、修订及当前脚本字节仍相同后重跑，保留后端的过期拒绝。真实 DeepSeek `deepseek-flash` 共 16 次 HTTP 200，模拟 0 次。十一套隔离 S3 均退出，六套 PostgreSQL 临时库已删除。阶段前后 C 盘可用分别为 23,622,705,152 B 和 23,578,517,504 B，Docker 构建缓存回收 0 B。 [CadQuery 重试及重跑](../../tmp/live-cli-startup-tfyiu0y0/job-repeat-geometry.json) · [复验与清理汇总](../../tmp/job-retry-capacity-validation-20261121.json)

工作流重试和显式检查点续算的输入封存仍按 RB-12、RB-14 核对。公开文件系统重试及两类存储按当前输入重跑见本页对应验收。

## 2026-11-21 正式 HTML 与 DOCX 报告的 S3 提交

公开 `/report:build` 先结束短 SQL 恢复事务。之后在锁外选择草稿、工程证据和 Pandoc 解析出的资源，按版本读入实际用到的 Markdown、CSS、图片和参考 DOCX。预检保留缺失候选、已选文件版本、适用的草稿发布及其他上游发布记录。Pandoc 可执行文件的准备阶段摘要使用 1 MiB 分块。 [输入准备](../server/app/services/report_handler.py) · [资源解析](../server/app/services/report_artifacts.py)

正式报告与草稿复用算例租约和封存日志。正文上传在写事务外完成，短事务复核权限、修订、配额、文件版本、工具和发布依赖，提交作业、封存清单和发布记录。同一幂等键继续支持此前登记的正式报告请求。文件系统与工作流入口仍使用已有提交协调流程，报告资源读取复用按需解析。 [封存协调](../server/app/services/report_build_staging.py) · [入口](../server/app/routers/reports.py)

| SQLite 与认证 SeaweedFS | 旧版本地暂存正文 | 新版本地暂存正文 |
| --- | ---: | ---: |
| 首次输入核验 | 13,107,724 B | 320,822 B |
| Pandoc 准备 | 12,802,748 B | 103,842 B |
| 写事务内再次读取输入正文 | 13,107,724 B | 提交复核文件版本 |

旧版每次完整视图都下载 12,582,929 B 的无关正文。旧版首次读取中独立 SQLite 写事务取锁失败。新版在同一测试位置可取写锁，同账户另一个算例保存成功，且原请求尚未返回。最终 HTML 和 DOCX 均嵌入实际 WSL OpenFOAM 求解、VTK 提取所得的 350 K 温度图。封存记录绑定已应用的 Markdown 草稿发布，原幂等键返回同一作业。 [旧版对照](../../tmp/live-cli-startup-o6yk3n2a/formal-report-capacity.json) · [SQLite 复验](../../tmp/live-cli-startup-o5yxfp5w/formal-report-capacity.json) · [专项入口](../server/live_llm_report_build_capacity.py)

PostgreSQL 与认证 SeaweedFS 通过另案并发写入。Pandoc 已解析旧稿后修改原稿，旧请求返回 409，作业列表没有新增记录。原幂等键重试封存新版摘要，HTML 包含改动，DOCX 保留实际图件。SQLite 文件系统的注册工程工作流仍可按证据链发布正式 HTML 与 DOCX。 [PostgreSQL 复验](../../tmp/live-cli-startup-9uvdesz7/formal-report-capacity.json) · [文件系统工作流](../../tmp/live-cli-startup-9e2c0kkp/engineering-report-schema.json)

认证 S3 的模板回归覆盖缺少引用时的拒绝、嵌套 CSS、自包含 HTML、参考 DOCX 的封存摘要以及修改样式后的历史版本下载。含基线实际 DeepSeek `deepseek-flash` 共 10 次 HTTP 200，模拟 0 次。六套隔离对象存储均退出，两套 PostgreSQL 临时库已删除。验收前后 C 盘实际可用空间分别为 23,632,306,176 B 和 23,623,602,176 B，本阶段 Docker 构建缓存回收量为 0。 [模板回归](../../tmp/live-cli-startup-g68wdtmp/engineering-report-schema.json) · [验收汇总](../../tmp/formal-report-build-validation-20261121.json)

首轮 SQLite 已通过并发探针，随后因租约对象缺少字典修订字段而返回 500。现从已经授权且完整加载的算例对象核对修订。同期启动的 PostgreSQL 测试跨过了该次源码修改，旧 API 调用新签名失败。两组均保留失败记录，新进程复验通过。这两次失败发生在模型回读前。 [SQLite 首轮](../../tmp/live-cli-startup-k3lo08em/live-result.json) · [PostgreSQL 首轮](../../tmp/live-cli-startup-a3d6f5x6/live-result.json)

续算和工作流恢复的原有分支仍在 SQL 事务内复制并封存旧输入。这些入口、注册工作流提交和其他长 I/O 仍属于 RB-12、RB-14。 [重试入口](../server/app/services/job_execution.py)

## 2026-11-21 S3 报告草稿的输入封存

公开 `/report:compose` 的两个 Markdown 输出共用一条 S3 提交路径。在算例租约内、SQL 写事务外解析已发布的工程输入，暂存草稿和实际选中的文件，先写入可恢复的封存草稿，再按校验后的大小和摘要上传文件。短事务复核访问权限、算例修订、原稿条件、源文件版本和发布依赖的数据库记录，绑定作业、不可变输入清单和恢复见证。worker 领取作业后按需建立执行目录。 [报告入口](../server/app/routers/reports.py) · [报告封存](../server/app/services/report_input_seal.py) · [共享封存](../server/app/services/command_input_seal.py)

| 实测环境 | S3 正文上传完成、作业提交前的旧草稿本地副本 | 并发写入 |
| --- | ---: | --- |
| SQLite 与认证 SeaweedFS，旧实现 | 3 份，共 37,748,862 B | 独立 SQLite 写事务无法取得写锁 |
| SQLite 与认证 SeaweedFS，新实现 | 1 份，共 12,582,954 B | 独立 SQLite 写事务取锁成功，同账户另一个算例保存成功 |
| PostgreSQL 与认证 SeaweedFS，新实现 | 1 份，共 12,582,954 B | 同账户另一个算例保存成功 |

表格统计上传完毕后的实际副本，上传过程中单个对象的短期暂存另计。三组 S3 算例均保存经实际 OpenFOAM 求解和 VTK 提取产生的后处理来源，发布后下载旧草稿并核对完整 SHA-256，相同幂等键返回原作业。 [旧版对照](../../tmp/live-cli-startup-bh38ilob/report-input-seal-capacity.json) · [SQLite](../../tmp/live-cli-startup-8orn4spi/report-input-seal-capacity.json) · [PostgreSQL](../../tmp/live-cli-startup-_28q9cxq/report-input-seal-capacity.json)

PostgreSQL 算例在 S3 确认写入后、作业提交前实际停止 API 进程。重启后核对原作业列表，原幂等键触发回滚，清除未绑定的输入对象并留下恢复记录，随后只创建和发布一条作业。SQLite 文件系统回归再次通过同案样式变化拒绝、原条件校验和读超时排空，返回 504 后可按原键重试。认证 S3 回归验证，同案写入无关文件仍可发布，增加优先样式表使原请求返回 409 且没有新增作业，原键重试绑定新样式。包括基线在内共 10 次真实 DeepSeek `deepseek-flash` HTTP 200，模拟 0 次。四套隔离 SeaweedFS 均退出，临时 PostgreSQL 数据库已删除。 [恢复与模型回执](../../tmp/report-input-seal-validation-20261121.json) · [文件系统回归](../../tmp/live-cli-startup-tzi8_z_n/compose-preparation-capacity.json) · [S3 来源回归](../../tmp/live-cli-startup-sdj4s_nr/compose-s3-projection.json) · [专项入口](../server/live_llm_report_input_seal_capacity.py)

RB-12、RB-14 继续核对正式文档工作流、重试及其他长 I/O 入口的写锁、暂存峰值和共享期限。

另行核对封存计划的内部路径校验。只有生成且不可执行的便携权限描述可随输入封存，其他保留路径和可执行描述均被拒绝。旧 WSL 权限回归套件在启动前发现源码审查摘要失效，按现行审查规则保留待复审状态。此次协议核对未发起模型请求。 [计划校验记录](../../tmp/report-input-seal-mode-protocol-20261121.json)

## 2026-11-21 S3 报告草稿的按需文件暂存

草稿准备和发布前的来源复核按实际读取路径暂存。原报告资源解析器按优先顺序读取 caseDict、已有草稿、证据、CSS、嵌套导入、DOCX 参考模板和已登记图件。读取保持 S3 版本条件和节点空间准入，同一个资源在一次扫描内只暂存一次。退出临时视图和提交作业时检查选中的版本和先前缺失的候选路径。兼容尚无逻辑目录的旧存储，旧文件通过精确对象 HEAD 选择。[临时视图](../server/app/services/case_read_view.py) · [草稿准备与复核](../server/app/services/compose_input_preparation.py) · [资源解析](../server/app/services/report_build.py)

| 实际 SeaweedFS 运行 | 草稿准备暂存正文 | 首次来源复核暂存正文 | 缓存路径 |
| --- | ---: | ---: | ---: |
| PostgreSQL 旧版对照 | 12,803,966 B | 12,803,966 B | 60 |
| SQLite 改造初轮 | 104,011 B | 100,632 B | 8 |
| PostgreSQL 最终复验 | 103,659 B | 100,280 B | 8 |

旧版真实对照随每轮扫描下载 12,582,949 字节的无关算例文件。改造后用独立下载核对该文件的长度及摘要，草稿的暂存记录只登记所用文件。同案在准备暂停期间写入无关文件，原报告仍正常发布。增加原来缺失的优先样式表，旧请求返回 409，且无新作业。原键重试绑定新样式，新发布仍引用实际 OpenFOAM 和 VTK 的温度图。[旧版对照](../../tmp/live-cli-startup-cj7le_tm/compose-s3-projection.json) · [SQLite 认证 S3](../../tmp/live-cli-startup-_uam43e_/compose-s3-projection.json) · [PostgreSQL 最终复验](../../tmp/live-cli-startup-wothlxr5/compose-s3-projection.json) · [专项入口](../server/live_llm_compose_s3_projection.py)

旧 S3 验收使用独立认证 SeaweedFS 和无逻辑目录的原格式对象，覆盖读取空正文、无关对象变化和缺失候选出现后的拒绝，实际临时缓存已删除。SQLite 文件系统复跑仍通过样式更新冲突、提交条件及 504 排空后重试。[旧 S3 兼容](../../tmp/compose-legacy-s3-7lfxlxwf/legacy-s3-selection.json) · [文件系统回归](../../tmp/live-cli-startup-0wrsqyhf/compose-preparation-capacity.json)

四组工程算例含旧版对照共 8 次真实 `deepseek-flash` HTTP 200，模拟 0 次。两套临时 PostgreSQL、四套临时 SeaweedFS 已清理。C 盘阶段前后分别可用 23,644,745,728 和 23,640,576,000 字节，Docker 构建缓存回收为 0。报告作业真正的输入封存和其余长 I/O 入口仍纳入资源预算工作清单。[模型与清理回执](../../tmp/compose-s3-selection-validation-20261121.json)

## 2026-11-21 报告草稿的模板依赖读取

`POST /report:compose` 在事务外读取草稿原稿、模板、嵌套 CSS 和已登记图件。前置协调先处理旧文件事务，释放 SQL 事务和账户预留后才开始准备。草稿原稿以 64 KiB 分块求摘要。准备使用已有有界读取线程池和期限，客户端断开时等待已开始的 I/O 排空。两条草稿输出共用同一路径。[入口](../server/app/routers/reports.py) · [准备见证](../server/app/services/compose_input_preparation.py)

提交前重新验证权限、字典修订、草稿条件、模板候选及已选依赖的文件版本。准备时缺失的更高优先级候选路径也纳入复核。封存的文件摘要须和准备时观察到的字节一致，随后提交作业、发布声明和投递。相同幂等键优先回读已登记结果。

在实际 80 单元 OpenFOAM、VTK 和报告流程上暂停 4,194,330 字节 CSS 的真实读取。修复前独立 SQLite 连接无法取得写锁。修复后独立写锁和同账户另案保存均成功。同案替换样式后，持旧输入的请求返回 409 且没有新作业，原幂等键重试封存新样式，重放复用原作业 ID。[旧入口](../../tmp/live-cli-startup-4va1ttbg/compose-preparation-capacity.json) · [SQLite 验收](../../tmp/live-cli-startup-_ei1_sbb/compose-preparation-capacity.json) · [专项入口](../server/live_llm_compose_preparation_capacity.py)

PostgreSQL 与认证 SeaweedFS 同样通过另案并发保存、同案源变化拒绝和原键重试。SQLite 另将隔离准备期限缩短为 1.5 秒。已开始的正文读取在过期后继续等待排空，随后返回 504，未创建作业，原键重试成功。[认证 S3 初轮](../../tmp/live-cli-startup-1lkhdl9a/compose-preparation-capacity.json) · [期限和排空](../../tmp/live-cli-startup-asl9uxle/compose-preparation-capacity.json)

认证 S3 最终复验还覆盖三个原有提交条件。草稿摘要不符、未确认覆盖有内容的草稿、字典修订不匹配均返回 409，均无作业副作用。两组临时 PostgreSQL 库和 SeaweedFS 进程已清理。阶段共 10 次真实 `deepseek-flash` HTTP 200，模拟 0 次。C 盘阶段前后可用空间分别为 23,645,417,472 和 23,645,962,240 字节，本轮 Docker 构建缓存回收为 0。[认证 S3 最终复验](../../tmp/live-cli-startup-xj9prmei/compose-preparation-capacity.json) · [模型与清理回执](../../tmp/compose-preparation-capacity-validation-20261121.json)

报告现有工程证据门禁和作业封存仍使用提交期协调。认证 S3 的准备阶段沿用已有版本化临时视图，相关封存和临时视图峰值继续按 RB-12、RB-14 核对。

## 2026-11-21 S3 类型作业的输入封存

默认整案输入的网格、求解、几何、转换和 ParaView 命令，先在短事务取得持久算例租约，再在 SQL 写事务外下载、核验并上传封存正文。每次条件 PUT 按实际字节独立准入。SQL 提交复核权限、存储所有者、字典版本、原始输入和已上传版本，绑定数字 Job ID、封存清单、提交见证和投递记录。作业领取后复用现有 S3 冷启动路径来建立本地输入与运行目录。[暂存和提交](../server/app/services/command_input_seal.py) · [封存恢复](../server/app/services/attempt_seal_journal.py)

| 验收 | 实测 | 证据 |
| --- | --- | --- |
| 上传期间并发 | 修复前，认证 SeaweedFS 接收 24 MiB 输入后暂停，独立 SQLite 写事务无法取得锁，同账户另案保存此时无法完成。修复后两种数据库均取得独立写锁，另案先完成保存，原作业随后运行并发布同一 SHA-256。 | [修复前](../../tmp/live-cli-startup-j9zqu94q/typed-command-source-result.json) · [SQLite](../../tmp/live-cli-startup-4e688y82/typed-command-source-result.json) · [PostgreSQL](../../tmp/live-cli-startup-adebi_hw/typed-command-source-result.json) |
| 确认后的 API 退出 | 两种数据库的真实 API 进程均在 S3 写入及额度确认后退出。重启后无作业记录，原幂等键重试将旧草稿和已知对象回滚，再创建、运行并发布一条作业。 | [SQLite](../../tmp/live-cli-startup-mmvjdazt/typed-command-source-result.json) · [PostgreSQL](../../tmp/live-cli-startup-og04hmbr/typed-command-source-result.json) |
| 确认前的 API 退出 | 两种数据库均验证真实 S3 已接收对象但 SQL 尚无写入回执的故障。重试返回 409 `file_recovery_required`，保留待决记录、受保护对象和 24 MiB 未结算额度。 | [SQLite](../../tmp/live-cli-startup-mmvjdazt/typed-command-source-result.json) · [PostgreSQL](../../tmp/live-cli-startup-og04hmbr/typed-command-source-result.json) |
| 期限与原入口 | 15 秒隔离期限到达后等待已开始的实际 PUT 返回，发回 504 `command_seal_timeout`，原键重试可清理旧草稿并正常发布。提交前源发生变化仍返回 409，文件系统同类命令继续通过实际发布。 | [封存超时](../../tmp/live-cli-startup-_zcwe73t/typed-command-source-result.json) · [SQLite 源冲突](../../tmp/live-cli-startup-k8a4h58p/typed-command-source-result.json) · [PostgreSQL 源冲突](../../tmp/live-cli-startup-vebkkgw0/typed-command-source-result.json) · [本地文件系统](../../tmp/live-cli-startup-yi0pfc56/typed-command-source-result.json) |

提交仍在事务内执行 S3 版本与封存元数据操作。显式输入、重试及专业工作流入口继续按 RB-12、RB-14 原验收范围核对。隔离验收的初次启动在 Windows 上以连接重置报告 API 冷退出，夹具已增加对此错误的识别。另一轮暂停测试将自身计时文件计入写入次数，已修正并复验。[首次冷退出记录](../../tmp/live-cli-startup-zfduaj6b/live-result.json) · [首次计时记录](../../tmp/live-cli-startup-pbtggeo7/live-result.json)

本轮真实 DeepSeek `deepseek-flash` 共 26 次 HTTP 200，用于通过平台 Agent 读回各轮实测证据。模拟模型调用 0 次。超时用例的业务和模型均通过，专用 SeaweedFS 的启动包装器等待退出超时，已独立确认原进程退出。14 个专用存储进程的原身份均已退出，最终两轮 PostgreSQL 专用库已删除。C 盘可用空间从 22.025 GiB 到 22.052 GiB，Docker 构建缓存仍为 22.67 MB。该阶段 Docker 回收量为 0，定时清理下次为本地 03:00。[验收和模型统计](../../tmp/command-seal-validation-20261121.json) · [专用存储退出核对](../../tmp/command-seal-stores-exited-20261121.json)

## 2026-11-20 S3 类型作业的提交前读取

独立类型作业选用默认整案输入时，在释放初始数据库会话的事务和取得账户写入额度之前读取 S3 正文及摘要。源观察记录绑定对象存储、算例前缀和完整可见文件版本。回到提交事务后重新核对发起者权限、账户并发额度、幂等键和原版本。实际封存结果还需与原字节记录一致、完整目录版本未变，才能一并提交作业、输入见证和投递记录。[提交入口](../server/app/routers/jobs.py) · [类型作业准备](../server/app/services/case_command_handler.py) · [封存见证](../server/app/services/job_execution.py)

| 验收 | 实测 | 证据 |
| --- | --- | --- |
| 修复前 | 认证 SeaweedFS 正在核对实际 24 MiB 正文时，独立 SQLite 写事务无法取得写锁，同账户另案保存等待读取放行。 | [基线](../../tmp/live-cli-startup-yhddw3fl/typed-command-source-result.json) |
| 修复后 | SQLite、PostgreSQL 均能在同一暂停点取得独立 SQL 写锁，实际同案保存也能提交。原请求返回 409，查询不到新作业。 | [SQLite](../../tmp/live-cli-startup-0dv3y11p/typed-command-source-result.json) · [PostgreSQL](../../tmp/live-cli-startup-kv0myjh3/typed-command-source-result.json) |
| 实际封存后源变化 | 两种数据库均暂停在封存完成与提交之间，从对象存储加入原选择之外的根目录。原提交返回 409，保留已登记的封存日志。按同键重试后旧日志清理，新作业实际运行、发布并读回原摘要。 | [SQLite 封存恢复](../../tmp/live-cli-startup-0dv3y11p/typed-command-source-result.json) · [PostgreSQL 封存恢复](../../tmp/live-cli-startup-kv0myjh3/typed-command-source-result.json) |
| 期限与兼容 | 隔离 SQLite 将读取期限设为 15 秒，已开始的正文读取先排空，再返回 504。作业列表及待决封存为空，同键重试正常。另有本地文件系统的实际执行、封存及发布回归。 | [读取超时](../../tmp/live-cli-startup-pegat3uz/typed-command-source-result.json) · [本地文件系统](../../tmp/live-cli-startup-rjyps94d/typed-command-source-result.json) |

当时的交付覆盖提交前的整案正文读取。真实 `deepseek-flash` 证据回读共 14 次，全部 HTTP 200，模拟 0 次。[模型回读](../../tmp/live-cli-startup-pegat3uz/live-result.json) · [后续输入封存](#2026-11-21-s3-类型作业的输入封存)

2026-11-06。N4-05 / RB-12 增量。主线完成 33/53、剩 20 项；资源清单完成 11/17、剩 6 块。全功能 295 项的完整交付范围不变。本批 130 条实际检查通过，不将检查数计为已交付任务。

## 修改与边界

新增 `app/services/case_write_capacity.py` 的 `committing`，用于已经审查的独立提交事务。先取得项目所有者存储额度，再进入算例协调器；SQL 提交及本事务产生的文件日志清理完成后才释放额度。异常回滚和提交/清理均等待实际操作结束。提前返回的重复请求也经过事务收尾。

几何、CalculiX、SU2、MPI、ParaView、后处理、报告、PPT、PDF、LaTeX、SyncTeX 和通用 Job 提交已采用该边界。旧 admission 的三个产品调用方（UDF、FloTHERM、批次）在准备范围内完成提交，移除了范围外的重复提交。批次文件日志清理也在额度释放前完成。原生准备、worker 启动、批次启动和投递均在该范围退出后执行；报告/LaTeX 的两个准备阶段分别拥有独立事务。

该 helper 会提交完整事务，不能嵌入更大的原子工作流。报告草稿的文件哈希/版本校验仍使用内层 `coordinate`，只由外层 `committing` 提交。收尾审查发现自动替换误将这处内层也改成 `committing`，已在归档前纠正，并补充实际草稿生成验证。

封存输入的存储配额改按工作区所有者计算，执行权限和作业额度仍属于发起用户。共享成员个人存储额度为零时，可以在有额度的共享工作区提交；所有者额度为零时，新提交被拒绝。实际 FS 测试发现旧封存恢复可能先增加元数据、再拒绝新封存，因此在进入 FS 协调器前增加所有者现有用量检查。该修复证明已耗尽额度的拒绝路径；少量正余额下恢复元数据的完整峰值预算尚待证明。

独立的成果别名修复事务使用相同提交边界。等待报告草稿完成后的读取使用已有的读取额度边界，不在等待原生执行期间持有事务或所有者额度。

## 实际验收

显式入口为 `platform/server/submission_capacity_checks.py`，组名为 `admission`、`native`、`sharing`、`compose`。实际运行 SQLite/PostgreSQL、FS/版本化 SeaweedFS S3 和产品原生工具。观察包装始终转发真实函数，只检查额度是否仍持有、或交接时已经释放。额度拒绝通过降低实际配额触发，不伪造用量或成果。

已审查隔离 API、S3、临时 PostgreSQL/迁移辅助代码和导入的函数；未运行历史模拟回归。报告 compose 是产品自身根据文件组织章节的确定性功能，没有替代任何模型调用。本批 **LLM 调用 0 次，模拟 LLM 0 次**，不作为模型能力验收。先前真实模型请求及失败仍以 [真实 Agent 记录](agent-memory-stream-lifecycle.md) 为准。

| 组 | 存储 / 数据库 | 最终检查记录 | 条数 |
| --- | --- | --- | --- |
| 零配额/缺失输入 | S3 / SQLite | `tmp/submission-capacity-09z108ww/checks.json` | 27 |
| 零配额/缺失输入 | S3 / PostgreSQL | `tmp/submission-capacity-du1zkq0_/checks.json` | 27 |
| 原生执行 | FS / SQLite | `tmp/submission-capacity-_gmp02ch/checks.json` | 11 |
| 原生执行 | FS / PostgreSQL | `tmp/submission-capacity-8g_njxwu/checks.json` | 11 |
| 原生执行 | S3 / SQLite | `tmp/submission-capacity-b7j8l6n2/checks.json` | 11 |
| 原生执行 | S3 / PostgreSQL | `tmp/submission-capacity-f7ey_p_x/checks.json` | 11 |
| 共享所有者 | FS / SQLite | `tmp/submission-capacity-odp6d5ug/checks.json` | 4 |
| 共享所有者 | FS / PostgreSQL | `tmp/submission-capacity-phapolk2/checks.json` | 4 |
| 共享所有者 | S3 / SQLite | `tmp/submission-capacity-xhhgwp5o/checks.json` | 4 |
| 共享所有者 | S3 / PostgreSQL | `tmp/submission-capacity-ihttyxk3/checks.json` | 4 |
| 草稿事务与等待 | FS / SQLite | `tmp/submission-capacity-wq5aui4f/checks.json` | 4 |
| 草稿事务与等待 | FS / PostgreSQL | `tmp/submission-capacity-7456j09p/checks.json` | 4 |
| 草稿事务与等待 | S3 / SQLite | `tmp/submission-capacity-2vfsgvg2/checks.json` | 4 |
| 草稿事务与等待 | S3 / PostgreSQL | `tmp/submission-capacity-x14w5fnj/checks.json` | 4 |

19 个提交入口在零额度下拒绝，不产生作业或物理用量增长；7 个缺失输入路径仍返回 404 并结算额度。原生组实际运行 shell、两条批次命令、CadQuery、Pandoc、PPT、pdflatex、PDF 编号、SyncTeX、CalculiX，并核对重复请求及成果。共享组实际封存并读取成员提交的文件；草稿组核对冲突不创建作业、旧稿逐字节备份、同步等待结果、原哈希变化后的重复请求和独立输出渠道。

所有最终记录通过且隔离服务已结束；9 个临时 PostgreSQL 数据库的删除回执纳入阶段证据。开发库和端口 8000 未操作。S3/PostgreSQL 原生批次有一次来源正在校验的冲突警告，持久重试后两条作业成功；这不是日志完全无警告的验收。最终 API 日志未出现未处理异常栈。

## 保留失败与局限

- `submission-capacity-21f5qdra`：初始几何夹具仍是返回空列表的默认脚本，真实发布失败。随后写入实际 CadQuery 长方体并重验，不把失败抹去。
- `submission-capacity-82pyjinm`、`submission-capacity-ky88txoq`：FS 共享所有者零额度的物理用量不变断言实际失败，定位到先发生的旧封存恢复；新增 FS 前置检查后双库复验通过。
- 较早 FS 原生通过记录 `8qdaii1w`、`t4bni7ma` 不重复计数，以前置检查修复后的最终记录为准。
- SU2、MPI、ParaView、UDF、FloTHERM 和注册工程后处理本批验证了对应拒绝路径，没有重跑全部成功执行矩阵；历史证据不扩大成本批新结论。
- 仍需补齐 CLI 设置、PTY、可视化会话等准备入口的完整调用链和资源生命周期；技能/远程导出、少量剩余额度下的 FS 恢复峰值，以及上传/派生的跨 worker 和进程死亡矩阵继续。
- RB-14 至 RB-17 的长 I/O 期限/有界队列、维护与扫描、全局并发、工作目录外资源强制限制仍待完成。正式网站认证联调按用户决定暂缓。

阶段源归档为 `platform/backups/mainline-submission-capacity-20261106.zip`，证据索引为 `tmp/mainline-submission-capacity-evidence/validation.json`。源哈希绑定与后续具体任务记录于 `tmp/submission-capacity-pending.json`。
