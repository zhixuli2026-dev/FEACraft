# 作业输入封存的准备日志与提交恢复

2026-11-05，N2-06 第十一批。为作业封存补上“准备目录已写入、数据库结果尚未确认”的恢复协议，覆盖普通输入、历史版本重试、发布引用和处理器生成输入共用的封存入口。主线仍为 23 完成、11 进行中、19 待开始，剩余 30；原 53 项和 295 项功能范围保持。

## 协议和行为

原封存先在 `.vibeflow/attempts/.preparing-{attempt_id}` 写入 inputs/run/manifest，再移动到最终 attempt 目录；外层事务提交结果未知时没有持久关联记录。本批在复制前固定最终 manifest，并把包含 case、Job、attempt、存储绑定及 manifest 的计划写到 `.vibeflow/attempt-provisions/pending/{attempt_id}.json`。封存内容和数据库 manifest 一致后，同一 SQL 事务追加 `attempt.inputs_sealed`，绑定计划 SHA-256。

恢复持有原算例锁。清单与 SQL 证明匹配时仅确认提交，不重写已经使用的执行目录或重放作业。无证明且 attempt 行也不存在时，先核对准备目录、最终目录及远端对象中的全部现有文件，再删除清单内摘要未变的内容。部分写入、额外文件、来源变化、损坏清单、存储绑定变化、已有 attempt 但缺少证明均保留并报告。自身尚未提交的 Session 记录识别为活动事务，不能提前恢复。

文件系统清理对两个候选目录先整体检查，之后删除匹配文件，仅移除清单推导出的空父目录；S3 先测量条件操作支持，删除使用对象版本条件。S3 读取缓存与远端存储仍分开：新缓存 API 能恢复远端计划，但不会跨主机删除旧节点的本地缓存。

异常和重复请求取消不再直接按目录递归删除已经记录计划的输入。取消仍等待文件复制线程排空，然后回滚数据库；下一次恢复按证明处理残留。保留原 10,000 文件、1 GiB 输入清单限制、双副本配额检查、来源版本/发布引用验证及原生启动约束。没有新增表或迁移开发数据库。

完成后保存确定的恢复回执，再删除待恢复清单。计划和回执位于受保护的执行元数据路径，普通保存/删除/上传不能修改，算例/工作区复制与备份均排除该目录，备份导入也拒绝携带它。

## 接口与自动恢复

| 入口 | 行为 |
| --- | --- |
| `GET /api/v1/cases/{id}/attempt-preparations` | 当前拥有者的只读诊断，返回计划的 SQL 判定及没有 SQL 所有者/准备计划的旧 attempt 目录 |
| `POST /api/v1/cases/{id}/attempt-preparations:recover` | 持有算例锁后恢复，报告 committed、rolled_back 或 recovery_required |
| 再次封存 | 在原算例锁内先处理既有计划 |
| 存储后台维护 | 启动和周期任务在算例锁内恢复计划；账户手动维护也包含 attempts 结果 |

没有证明的旧目录仅报告 `untracked_retained`，不能据文件存在推断执行成功或授权重算。原执行器仍在实际启动时核验封存输入；这里的 committed 表示 SQL 提交得到确认，不等价于对后来所有运行文件重新做完整验证。

旧 SeaweedFS 后端的已有算例仍能运行原普通作业；不支持条件删除时保留恢复日志和文件并报告需处理，不绕过版本保护进行清理。新建算例等已有后端能力要求保持。

## 验证

| 范围 | 通过 | 原始证据 |
| --- | --- | --- |
| 文件系统 + SQLite：计划后、复制中/后、提交前/后、确认丢失、外部文件 | 7 组 | `../../tmp/seal-recovery-evidence-_qetkxpm/checks.json` |
| S3 + SQLite，同范围并使用新 API 缓存 | 7 组 | `../../tmp/seal-recovery-evidence-70q5m69i/checks.json` |
| 文件系统 + PostgreSQL，同范围 | 7 组 | `../../tmp/seal-recovery-evidence-ruchwmc5/checks.json` |
| S3 + PostgreSQL：复制中、提交前/后、确认丢失、外部文件 | 5 组 | `../../tmp/seal-recovery-evidence-hb0u9pvv/checks.json` |
| 无证明旧目录、损坏绑定、已有 attempt 缺少提交证明 | 文件/S3 各 3 组 | `../../tmp/seal-recovery-evidence-nt8p46_n/checks.json`、`../../tmp/seal-recovery-evidence-gnxnr4ig/checks.json` |
| 真正启动维护自动恢复，重启后不调用手动恢复推进 | 文件/S3 各 1 组 | `../../tmp/seal-recovery-evidence-u0tjladk/checks.json`、`../../tmp/seal-recovery-evidence-w4sftw77/checks.json` |
| 原封存回归：原生运行、重复取消排空、来源变化、配额、调度和复制/备份 | SQLite/PostgreSQL 各 23 项 | `../../tmp/attempt-workspace-8xpa7qjp/sqlite/result.json`、`../../tmp/attempt-workspace-b_0q__mg/result.json` |
| 真实 S3 UDF 集成、重放检查、空缓存、当前及历史发布引用 | 1 组 | `../../tmp/s3-attempt-store-hj3ki7qh/professional-result.json` |
| 旧 SeaweedFS 已有算例普通运行与拒绝不受支持的清理 | 1 组 | `../../tmp/s3-attempt-store-5i532qwv/seal-legacy-result.json` |

26 组提交窗口、6 组保护边界和2组启动恢复合计34组。PostgreSQL 测试临时库均已删除。汇总、失败日志、源码绑定在 `tmp/mainline-N2-attempt-seal-evidence/`。

最初原封存测试断言异常后立即删除目录，已改为先检查数据库回滚与残留保留，再调用正式恢复验证清理；部分来源变化则验证保留不可确认的副本。两库连跑还暴露测试内导入服务保留旧 SQLite SessionLocal 的问题，已让全部导入工厂切换到当前测试库；PostgreSQL 单独进程的23项通过。失败时遗留的唯一命名临时库在确认无连接后删除，证据为 `tmp/seal-compat-db-cleanup.json`。

原 S3 UDF 的一次409仍未归因。本次真实流程通过只能证明当前回归成功，不能证明旧故障根因已经修复；没有修改来源校验或放宽过期拒绝。

## 后续范围

继续核对旧专业准备/封存/兼容文件补偿、未知提交的其他路径、S3旧节点本地缓存、准备前的临时来源视图和日志写入残留。缺少归属或内容证明的部分文件需要诊断及后续人工处置入口，不能自动删除。跨主机全矩阵、索引对账、完整恢复界面与审计、原UDF409仍按原 N2/N5/N6/N7 要求保留。本批不关闭整项。

## 2026-11-19 后台封存恢复分页

启动和定期的存储维护，以及 `POST /api/v1/case-provisions:maintenance`，每次对一个算例处理同一阶段内最多 32 个封存候选。Windows 文件系统依次核对写入意图与待决封存清单。认证 S3 依次核对远端待决清单与本机遗留缓存。候选在算例锁外按直属名称选择，文件系统的 `.json` 意图和 `.part` 数据占用同一个游标位置。游标绑定算例、存储配置与本机根，算例锁内复核并原子落盘，API 重启后从上次位置继续。定期回到起点重新观察留存的记录和后来新增的项。扫描、准入、恢复和进度写入使用现有的存储期限。[后台实现](../server/app/services/storage_maintenance.py) · [封存证据检查](../server/app/services/attempt_seal_journal.py)

封存清单、SQL 见证、活进程身份、源摘要和 S3 条件删除沿用既有判断。未知或不匹配记录保持原样。新作业提交和单算例手工诊断、手工恢复继续使用原完整核对入口和返回结构，当前后台游标只控制维护请求检查的顺序。

真实旧版 SQLite 与 Windows 文件系统对照，在公开 `POST /jobs` 完成实际输入复制且 SQL 尚未提交时终止隔离 API。第一次维护同时看到并处理四条候选，包括活写者、无归属 `.part`、损坏清单和已退出写者对应的可回滚清单，未写出续页游标。旧对照执行器 `1/1`，10.469 秒。 [旧版对照](../../tmp/seal-maintenance-sqlite-fs-hos_rdtv/seal-maintenance-page.json)

固定测试使用每页 1 条并再次实际终止源 API。第一轮账户维护保留未知清单及后页原封存对象。两组文件系统也验证实际存活写者及无归属 `.part` 被保留。再次启动 API 时由实际后台维护读取原游标，只回滚证明匹配的晚页封存，SQL 没有残留作业，公开原文件按原字节读回。两组认证 S3 使用同一主机上的不同本地缓存根。远端清单回滚后，旧根的缓存直到其自己的第三页才按已有回执实际回收，无归属缓存保持原字节。

| 业务数据库与存储 | 正式维护和恢复记录 |
| --- | --- |
| SQLite 与文件系统 | [结果](../../tmp/seal-maintenance-sqlite-fs-wusn2ifr/seal-maintenance-page.json) |
| SQLite 与认证 SeaweedFS | [结果](../../tmp/seal-maintenance-sqlite-s3-zrr12byl/seal-maintenance-page.json) |
| PostgreSQL 与文件系统 | [结果](../../tmp/seal-maintenance-postgresql-fs-ddsgh39g/seal-maintenance-page.json) |
| PostgreSQL 与认证 SeaweedFS | [结果](../../tmp/seal-maintenance-postgresql-s3-mubznxuy/seal-maintenance-page.json) |

固定执行器使用 122 个精确源码，正式结果 `1/1`，161.844 秒。真实及模拟模型均为 0。独立核验了 16 个 API 进程及各自端口、两个测试写者进程、两套 SeaweedFS 进程及其 16 个端口全部结束，也核对了两个临时 PostgreSQL 数据库的删除回执。暂存和证据在 D 盘。固定版前后 C 盘可用空间 22.02 → 22.01 GiB，Docker 构建缓存为 0 B。此次证明的是后台与账户维护的封存恢复分页，RB-15 的账户历史诊断仍按原范围验收。 [固定执行器](../../tmp/vibeflow-regression-all-20261119-204034-4ba9f6/summary.json) · [独立关停](../../tmp/seal-maintenance-page-stage-20261119/os-exit-proof.json)

## 2026-11-19 单算例手工诊断与恢复分页

`GET /cases/{id}/attempt-preparations` 和 `POST /cases/{id}/attempt-preparations:recover` 共用后台维护的封存阶段和证据检查。每次最多选择 32 个候选，可用 `limit` 缩小。结果跨页时，沿 `next_cursor` 继续。已有小算例无需分页时，公开响应仍为原来的 `results`，诊断另含 `untracked`。游标绑定调用账号、算例、工作区、实际对象存储及本机根，并区分只读与写入模式。当返回页中没有需要报告的候选时，仍需沿有效 `next_cursor` 继续。[公开入口](../server/app/routers/workspaces.py) · [分页与归属查询](../server/app/services/attempt_seal_maintenance.py)

只读页先选实际候选，再核对这批 JobAttempt 的 SQL 所有权及对应的待决、历史记录。未登记文件只报告并保留。恢复页先在业务写锁外选择候选，取得算例锁后重查用户权限与实际存储绑定，逐个执行原 SQL 见证、文件摘要和条件清理。重复请求仍依相同见证判断，原提交决策沿用既有规则。正式作业提交继续走完整的封存安全检查。[现有见证与回滚](../server/app/services/attempt_seal_journal.py)

旧版 SQLite／Windows 文件系统对照用真实 Windows 作业提交一份正常输入，然后在下一份实际封存完成、SQL 尚未提交时终止 API。旧 GET 的 `limit=1` 同时返回四个检查及一个无归属目录，旧 POST 的 `limit=1` 同时处理四个候选并回滚后页。旧版执行器 `1/1`，12.968 秒。 [旧版对照](../../tmp/seal-manual-sqlite-fs-4vwsc__v/seal-manual-page.json)

固定版四组均以每页 1 个候选读取，在实际 API 退出后继续用原诊断游标，并从正式入口逐页恢复。四组都验证了已提交的 Job/attempt 和输入原样保持，未提交的封存只回滚一次，持久回执与原计划完全相同。两个 S3 组的远端与本机同名未知内容只报告一次，两份内容均保持。外部账户访问返回 404，跨算例与把只读游标用于恢复均返回 422。小算例原响应结构通过。

| SQLite／PostgreSQL 与真实存储 | 诊断／恢复页数 | 验收记录 |
| --- | ---: | --- |
| SQLite 与文件系统 | 7／4 | [结果](../../tmp/seal-manual-sqlite-fs-p5x_3zl1/seal-manual-page.json) |
| SQLite 与认证 SeaweedFS | 11／4 | [结果](../../tmp/seal-manual-sqlite-s3-9x92suar/seal-manual-page.json) |
| PostgreSQL 与文件系统 | 7／4 | [结果](../../tmp/seal-manual-postgresql-fs-6g47q3cc/seal-manual-page.json) |
| PostgreSQL 与认证 SeaweedFS | 11／4 | [结果](../../tmp/seal-manual-postgresql-s3-irhyazgq/seal-manual-page.json) |

固定执行器以 144 个精确源码完成 `1/1`，164.39 秒。独立核验旧版与固定版的 14 个隔离 API 及监听、5 个实际 Windows 作业身份、两个 SeaweedFS 进程及 16 个端口均已退出，两个临时 PostgreSQL 数据库已删除。本节真实及模拟模型调用均为 0。夹具与证据在 D，C 盘可用空间前后均为 22.01 GiB，Docker 构建缓存为 0 B。 [固定执行器](../../tmp/vibeflow-regression-all-20261119-211032-261d48/summary.json) · [独立关停](../../tmp/seal-manual-page-stage-20261119/os-exit-proof.json)

### RB-15 原条件核对

- 历史维护采用有界页。账户、算例、上传、构造、私有缓存、封存后台和正式手工诊断／恢复均有实际页证据。账户存储诊断另在双库双存储通过跨页、重启和账户隔离。 [后台与上传维护](storage-maintenance.md) · [账户诊断 PG/S3](../../tmp/provision-diagnosis-postgresql-s3-vq_r2gb7/diagnosis-result.json)
- 未知记录保留原额与原对象。账户预留轮转实测保留三笔合计 65,627 字节，算例预留轮转实测保留三笔合计 167 字节。不匹配的暂存和并发活写者继续保留。 [账户额度](owner-storage-admission.md) · [算例额度](remote-write-storage-admission.md)
- 可验证的预留继续按原绑定恢复及结算。双库真实远端写入丢回执后复核同一对象并结算，旧结清回执经过分批无损压缩、原记录读回及还原，原引用和对象版本保持。普通下载预留也通过真正终止后的正常准入回收。 [回执压缩与还原](reservation-history-compaction.md) · [下载预留恢复](manual-file-recovery.md#raw-下载退出后的旧预留复验)
