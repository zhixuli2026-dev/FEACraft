# 存储启动对账与残留保留

2026-11-19，启动与定期维护已接入[私有缓存分页、清单校验和停机关闭](private-cache-maintenance.md)。

## 2026-11-22 单页维护的期限和并发准入

账户维护的公开入口和定期任务共用单页绝对期限。构造恢复、上传枚举、封存扫描和结算沿用剩余时间。整页通过现有的四个存储操作名额和专用线程执行，满额返回 429。到期停止派发新算例，已启动的线程先排空再释放名额。已提交的逐项结果及持久游标可在重试时继续使用。 [实现](../server/app/services/storage_maintenance.py)

隔离测试将单页期限设为 4 秒，分别在实际上传目录和封存目录读取后暂停 2.5 秒。原文件系统和认证 SeaweedFS 各超过期限并返回 200。修复后，文件系统和认证 SeaweedFS 分别配 SQLite、PostgreSQL 的四组均在实际线程排空后返回 504，期间同算例保存返回 200。四个请求占用扫描线程时，第五个请求返回 429。释放后同页重试成功，未完成上传保持原偏移。隔离 API、认证 SeaweedFS 和临时库全部收尾。本项真实模型 0 次，模拟 0 次。 [原始结果与收尾证据](../../tmp/N4-05-maintenance-page-budget-20261122/validation.json)

## 2026-11-19 账户存储诊断分页

`GET /api/v1/case-provisions:maintenance` 支持 `limit` 和 `cursor`。每次只读取当前账户的一页，查询数据库时只检查本页候选是否仍被算例引用。小账户的单页结果仍可按原字段读取。多页响应的数量和字节是本页增量，合并时按 `unreferenced_inventory.prefix` 汇总。继续使用 `next_cursor`，直到返回空游标。只有首尾位于同一页的响应标记为 `complete=true`。游标属于当前账号及本次使用的存储配置，配置调整后应从首重扫。诊断不触发清理。 [诊断入口](../server/app/services/case_provision_diagnostics.py)

文件系统按路径顺序读取实际文件元数据，目录身份复核不经过链接或重解析点。SeaweedFS 按物理对象分页。标为 `inventory=physical` 的数量包含真实占用空间的版本数据和索引元数据。已登记算例不会列为无归属。单目录枚举超过配置的条目或时间上限时显式报错，避免将不完整清单当作完整结果。 [账户维护接口](../server/app/routers/workspaces.py)

真实 SQLite 与 FS 旧版在低于库存总量的扫描限制下返回 409。固定四组合使用七条的请求页，在第一 API 退出后由新的 API 完成余页。FS 各 22 页，认证 SeaweedFS 各 18 页。四组都汇总到九个保留前缀，已登记算例及其他账号的内容排除在外。另一个账号重放游标返回 422，真实文件回读保持。初次 S3 夹具将管理过的对象计作一个逻辑文件，复验通过独立的原始 S3 清单核对实际对象数、实际字节和索引元数据。模型调用为 0。 [旧版 409](../../tmp/provision-diagnosis-sqlite-fs-6s9rueql/diagnosis-result.json) · [SQLite 与 FS](../../tmp/provision-diagnosis-sqlite-fs-2xcmensw/diagnosis-result.json) · [PostgreSQL 与 FS](../../tmp/provision-diagnosis-postgresql-fs-5zc9ajq8/diagnosis-result.json) · [SQLite 与认证 S3](../../tmp/provision-diagnosis-sqlite-s3-ebt1fw3s/diagnosis-result.json) · [PostgreSQL 与认证 S3](../../tmp/provision-diagnosis-postgresql-s3-vq_r2gb7/diagnosis-result.json) · [原物理对象差异](../../tmp/provision-diagnosis-sqlite-s3-6qw2daia/diagnosis-result.json)

## 2026-11-19 算例内上传维护分页

每次按算例选出最多 32 个上传候选。在取得算例锁与账户存储额度前读取候选。SeaweedFS 使用带 `Delimiter` 的单页对象列表，直接查看上传目录名。文件系统只查看直接子项，用固定大小的堆保留一页候选，扫描时间仍取决于直接子项数。两种存储都避免通过枚举全部分块来找候选。算例私有游标在核对当前归属和完成回执后更新，重启后续扫，末页回到起点。FS 也继续识别 `.completing` 暂存目录。[维护实现](../server/app/services/storage_maintenance.py)

四种数据库与存储组合将页大小设为 2，各处理 7 个实际上传。每组确认暂停候选枚举期间同一算例及同一账户的两项实际写入均成功。终止并重启 API 后从保存的游标走到后页，两项已完成上传的暂存安全回收，发布文件逐字节回读。不完整、损坏和外部账户的暂存原样保留。S3 实测目录前缀与分页，FS 实测中断完成留下的 `.completing`。最终源码另通过 SQLite 与本地文件存储的隔离复验。[SQLite 与文件系统](../../tmp/live-cli-startup-0tc8fsfd/upload-child-maintenance.json) · [PostgreSQL 与文件系统](../../tmp/live-cli-startup-delgva67/upload-child-maintenance.json) · [SQLite 与 S3](../../tmp/live-cli-startup-bm5bkllp/upload-child-maintenance.json) · [PostgreSQL 与 S3](../../tmp/live-cli-startup-10h_r18n/upload-child-maintenance.json) · [最终源码复验](../../tmp/upload-maintenance-infrastructure-xwxsvf7u/infrastructure-result.json)

四组运行另使用真实 `deepseek-flash` 读回维护证据，8 次 HTTP 200、34,532 tokens、0 次模拟。临时 API、SeaweedFS 和 PostgreSQL 都已退出或删除。[复跑入口](../server/live_llm_upload_maintenance_child_acceptance.py) · [PostgreSQL 与 S3 的模型记录](../../tmp/live-cli-startup-10h_r18n/live-result.json)

## 2026-11-19 账户与算例分页维护

账户维护按算例 ID 每页处理最多 32 个算例，多取一条判断是否有下一页。首次请求记录当前账户最大的算例 ID。分段执行时，将 `case_scan.through_case_id` 和 `case_scan.next_after_case_id` 分别传入下次请求的 `through_case_id` 和 `after_case_id`。`next_after_case_id` 为 `null` 表示本轮完成。创建恢复、暂存和历史处理包含在起始页，后续页继续使用原 `uploads` 和 `attempts` 数组。请求始终按登录账户限制算例范围。[公开接口](../server/app/routers/workspaces.py) · [维护实现](../server/app/services/storage_maintenance.py)

后台每轮先记下当前最大账户 ID，在每组最多 32 个账户中交替处理各账户的算例页。单页完成即结算该页的逐算例处理。新建账号和超过该账户扫描上界的新算例由下轮维护接续。S3 的本节点旧缓存先用目录扫描预算收集实际候选，再按候选 ID 分批查询所属算例的 JobAttempt，每批最多 64 个。不完整的上传保留原续传状态，没有归属证明的缓存保留原文件。[本节点缓存对账](../server/app/services/attempt_seal_journal.py)

专项通过隔离 API 和真实文件系统或 SeaweedFS 验证。为了在少量算例中重现跨页行为，每页设为 2，数据库实际查询上限为 3（含判断续页的一条）。实际重启 API 后，维护顺序为首账户起始页、另一账户起始页、首账户后续页。单独发送外部账户的游标不会扩大该账户的扫描范围。两份未完成上传与 S3 未知缓存均保持原状。四组完成后，真实 `deepseek-flash` 通过文件读取工具读取验收摘要，共 8 次 HTTP 200，模拟调用 0。API、临时 PostgreSQL 数据库和临时 SeaweedFS 均已清理。[复跑入口](../server/live_llm_storage_maintenance_page_acceptance.py)

| 数据库与存储 | 实际 API 与重启 | 模型回执 |
| --- | --- | --- |
| SQLite 与文件系统 | [分页记录](../../tmp/live-cli-startup-dl8n5tg3/maintenance-pages.json) | [回执](../../tmp/live-cli-startup-dl8n5tg3/live-result.json) |
| PostgreSQL 与文件系统 | [分页记录](../../tmp/live-cli-startup-fum2biuv/maintenance-pages.json) | [回执](../../tmp/live-cli-startup-fum2biuv/live-result.json) |
| SQLite 与 SeaweedFS | [分页记录](../../tmp/live-cli-startup-2krnrme5/maintenance-pages.json) | [回执](../../tmp/live-cli-startup-2krnrme5/live-result.json) |
| PostgreSQL 与 SeaweedFS | [分页记录](../../tmp/live-cli-startup-nmi1wcgf/maintenance-pages.json) · [SQL 候选记录](../../tmp/live-cli-startup-nmi1wcgf/maintenance-trace.jsonl) | [回执](../../tmp/live-cli-startup-nmi1wcgf/live-result.json) |

此次覆盖算例页、账户扫描上界和缓存候选查询。账户内部历史与私有缓存记录的维护，以及结果未明的预留对账继续由 RB-15 跟踪。本次无数据库迁移。回退可恢复上一版 API 和服务实现，原有维护记录继续有效。[资源预算工作清单](resource-budget-worklist.md)

2026-11-16 更新：S3 当前文件的旧不可变版本已提供独立[停机回收和删除清单压缩工具](storage-version-operations.md)。本文后台维护继续按原规则运行，不自动执行该回收；下文阶段数字与验证均为历史记录。

后续更新：后台维护已同时处理作业输入封存准备日志，账户维护新增 `attempts` 结果；见 [封存准备恢复](attempt-seal-recovery.md)。下文保留第十批范围及证据。

2026-11-05，N2-06 第十批。已实现启动后台扫描、周期重试、创建暂存归属记录、按证明清理空目录、可配置的创建恢复历史保留，以及上传完成后的残留清理。主线 23 完成、11 进行中、19 待开始，剩余 30；本批不关闭完整 N2-06 或原 295 项功能要求。

## 运行与接口

API 完成基础初始化后启动后台维护任务，立即扫描，随后默认每 300 秒扫描一次。账户 ID 分页，每个账户独立事务；个别账户失败不会阻断其他账户。关闭时取消任务，并等待正在执行的文件操作完成后释放数据库锁。开发服务没有在本批重启，验证均使用隔离 API 和临时数据库。

创建恢复、暂存和历史清理先持有账户锁；完成这一事务后，再逐个获得算例锁，恢复其文件事务并检查上传残留。正在创建的算例不会被当作孤立文件清理。删除到回收站的算例跳过；原工作区/算例访问和内容写入仍按原授权与锁处理。

| 接口 | 用途 |
| --- | --- |
| `GET /api/v1/case-provisions:maintenance` | 当前账户的只读诊断：待恢复记录、恢复历史、暂存文件和字节数、目录占用记录，以及没有数据库引用的非空算例前缀与容量 |
| `POST /api/v1/case-provisions:maintenance` | 执行当前账户的维护页，返回创建、暂存、历史、上传和封存结果及算例游标 |
| `POST /api/v1/case-provisions:recover` | 原有创建恢复接口，保持兼容 |

诊断不会因为目录较旧或没有数据库行而删除内容。后台把待人工处理的数量和账户 ID 写入日志；持久创建记录和上传完成证明仍是重试依据。用户界面的统一恢复面板及完整恢复操作审计继续按原主线推进。

## 清理与保留规则

新创建写入的本地暂存文件使用随机 `.part` 文件和先写入的 `.json` 归属记录，记录存储绑定、创建时间、大小和摘要。正常写入完成立即移除暂存；进程终止后，超过保留期限且内容完整匹配的暂存文件可清理。部分写入、内容被改、缺失归属记录、格式损坏的旧暂存保留并报告。S3 条件写入不使用这套本地创建暂存目录。

创建回滚仍先核对全部现有文件，再删除清单中未变化的内容。新增空目录清理只尝试清单路径推导出的父目录，使用 `rmdir`；出现额外文件、额外目录或链接时保留现场。Windows 共享句柄冲突仅重试错误 32/33，最长两秒，每次重新检查路径，不回退到递归删除。

创建恢复历史默认永久保留。启用按天过期后，历史内容必须通过原创建清单验证；提交历史要求 SQL 持久事件的算例、类型、目录和清单摘要一致；回滚历史要求没有 SQL 提交证明、没有算例引用，也没有残留文件。仍有待恢复清单的历史不删除。目录占用记录和 SQL 提交证明始终保留，避免 SQLite ID 复用重新占用旧目录。S3 清理前测量条件操作支持，删除使用对象版本条件。

上传只有在找到匹配的 SQL 完成回执后才重试清理原分块/暂存；不重新提交目标文件，因此用户完成上传后的编辑不被覆盖。未完成上传继续保留以支持续传，不自动按时间取消。S3 保留原上传清理证明和取消标记，已证明归属的块按原条件删除。

| 环境变量 | 默认 | 含义 |
| --- | --- | --- |
| `VIBEFLOW_STORAGE_MAINTENANCE_ENABLED` | `true` | 是否运行启动及周期后台扫描；手动接口独立可用 |
| `VIBEFLOW_STORAGE_MAINTENANCE_INTERVAL_SECONDS` | `300` | 后台间隔秒数 |
| `VIBEFLOW_PROVISION_STAGE_RETENTION_SECONDS` | `86400` | 完整且有归属的本地创建暂存保留期限；最小 60 秒 |
| `VIBEFLOW_PROVISION_HISTORY_RETENTION_DAYS` | `0` | 创建恢复历史保留天数；0 为不自动过期 |

配置模板已同步。普通隔离测试明确关闭后台任务，以便原故障测试控制恢复时机；本批维护测试显式开启真实后台任务验证启动行为。

## 验收证据

| 范围 | 通过 | 证据 |
| --- | --- | --- |
| 文件系统 + SQLite | 7 项 | `../../tmp/maintenance-evidence-2e2xhwjj/checks.json` |
| 文件系统 + PostgreSQL | 7 项 | `../../tmp/maintenance-evidence-8dozhmlv/checks.json` |
| SeaweedFS 4.52 + SQLite | 6 项 | `../../tmp/maintenance-evidence-at0m8yrv/checks.json` |
| SeaweedFS 4.52 + PostgreSQL | 6 项 | `../../tmp/maintenance-evidence-ft6_mqpq/checks.json` |
| 原新建：清单/文件/提交边界、回滚和提交确认丢失 | 6 组 | `../../tmp/case-provision-evidence-fhso2vh_/checks.json` |
| 工作区复制/备份恢复：首个子算例及提交前终止 | 4 组 | `../../tmp/constructor-recovery-evidence-5v38r209/checks.json` |
| 原上传：正常、外部修改暂存、提交后终止 | 3 组 | `../../tmp/upload-transactions-evidence-y74rayw2/checks.json` |

维护验证覆盖真实进程终止后新 API 自动恢复、完整老暂存清理而部分/旧暂存保留、有效历史过期但目录占用及已提交内容保留、账户隔离、非引用目录保留、上传清理重试且保留后续编辑与未完成上传、历史与 SQL 证明不匹配时保留，以及第二个 API 等待活动创建者提交。历史过期使用测试时钟，未实际等待数日。两组 PostgreSQL 临时库均已删除。

首次维护测试在把 `.bin` 响应按文本解析时失败，改为核对真实原始下载内容后通过。原上传测试包装快照函数未转发新可选容量参数，已同步转发。原工作区回滚回归命中 Windows 目录共享冲突，加入定向重试后四组通过。失败日志和最终结果均归档在 `tmp/mainline-N2-storage-maintenance-evidence/`。

## 继续保留的范围

旧作业准备/封存/兼容目录及缺少准备证明的文件需要继续诊断与补偿；本批没有按时间删除无归属文件，也没有把未完成上传取消。跨主机及全部存储故障矩阵、索引对账、完整审计与恢复界面仍按原主线保留。原 S3 UDF 当前引用一次未归因的 409 继续定位。

文件系统的检查和删除不构成任意宿主写者之间的内核级比较交换；部分写入无法证明完整性时保留现场。后台首次扫描仍可能受存储或数据库不可用影响，会按周期重试并报告原因，不把这种情况标成已恢复。
