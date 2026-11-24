# 存储用量核验与扫描预算（N4-05）

后续更新：真实 `_trash/users/{user_id}/` 回收站计量与并发移动空窗现已修复并经双库验收；下文原范围限制保留历史语境。详见[回收站计量](trash-storage-accounting.md)。

本批修复用户存储配额漏算 S3 远端保留数据的问题，并把存量扫描移出事件循环。它是资源预算的一部分，N4-05 仍在进行中；测量结果不是并发写入预留，也不是操作系统磁盘硬配额。

## 原始缺陷与修复

原 `quota.py` 只在存在 Workspace 数据库行时，同步扫描本地 `workspaces/{user_id}`。真实 S3 中保留 2 MiB 的尝试输出、套餐上限 1 MiB 时，用量接口返回 0，提交准入返回 200。复现记录位于 `tmp/storage-usage-8i55456y/storage-usage-observation.json`，原失败日志为 `tmp/N4-storage-usage-before.log`。

新增 `app/services/storage_usage.py`，按实际配置的后端扫描用户前缀，无需数据库仍保留工作区行。本地计入该前缀下的普通文件，包括工作副本、历史发布、隐藏文件与未回收的失败产物。目录链接、Windows junction/reparse point、特殊文件、目录身份变化或无法读取时拒绝核验，不静默返回零。硬链接按路径计量，可能保守多计；文件大小是逻辑字节数，不是磁盘块占用。

S3 使用原始客户端枚举实际对象，绕过只展示逻辑文件的投影客户端。未启用版本控制时遍历 `list_objects_v2`；启用或暂停版本控制时遍历 `list_object_versions`，计入历史版本和 null 版本。删除标记占扫描条目但按零数据字节计量。所有 key 必须属于精确的 `workspaces/{user_id}/` 前缀，防止相邻用户 ID 混入。不下载对象正文。这里不包含存储服务副本、索引、对象元数据和未完成分段上传的服务端占用。

`quota.usage` 保留 `storage_mb_used`，新增精确的 `storage_bytes_used`。准入使用整数字节相加，拒绝负数和布尔增量；超限返回 429，无法核验返回 503。准入扫描的已知小计一旦超过允许剩余额度，可提前拒绝；这种不完整小计不会用于允许写入或用量展示。

## 扫描预算和取消

| 配置 | 默认值 | 用途 |
| --- | ---: | --- |
| `VIBEFLOW_STORAGE_MEASUREMENT_TIMEOUT_SECONDS` | 30 | 包含等候扫描槽位的时间预算 |
| `VIBEFLOW_STORAGE_MEASUREMENT_MAX_ENTRIES` | 1000000 | 文件、目录、对象版本及删除标记的条目上限 |
| `VIBEFLOW_STORAGE_MEASUREMENT_CONCURRENCY` | 4 | 单进程事件循环的最大同时扫描数 |
| `VIBEFLOW_STORAGE_MEASUREMENT_PAGE_SIZE` | 1000 | S3 每页条目请求数 |

扫描在 I/O 线程执行。取消请求后，必须等实际扫描线程结束才释放槽位；否则频繁取消会绕过并发上限。时间预算在扫描步骤之间检查，S3 请求另有连接/读取超时并关闭 SDK 自动重试。这不是强制终止阻塞的操作系统文件调用；在途调用可能超过扫描预算，接口等待线程排空。扫描并发是单进程限制，尚未实现跨节点总额度。

S3 凭据需具备读取 bucket 版本控制状态、列出对象及对象版本的权限。缺少权限、未知版本状态或分页异常都会导致 503，不回退到本地缓存。环境变量模板见 `platform/server/.env.example`；容器部署需把这些变量实际传入 API/worker 环境。

## 验收与兼容记录

`storage_usage_smoke.py --postgres` 使用隔离 HTTP 服务、临时 SQLite/PostgreSQL 数据库和真实 WSL S3 实例。覆盖本地保留文件、相邻用户隔离、junction 拒绝、精确字节边界、远端隐藏对象、分页、原生版本和暂停版本控制，以及慢扫描响应、取消排空、扫描上限、超时和后端错误。最终结果及与源码快照的绑定另记于 `tmp/mainline-storage-usage-evidence/`；没有结果前不视为整套通过。

首次组合运行在旧 SeaweedFS 3.99 的原生版本分页处失败：后端重复返回同一 continuation marker，SDK 抛出 PaginationError；产品正确返回 503。独立诊断见 `tmp/s3-attempt-store-kpupqcmc/version-accounting-diagnosis.json`。没有通过跳过 key 或只计当前版本来绕过错误。

固定的 4.52 测试运行时对同一诊断返回 33 字节、8 个条目，见 `tmp/s3-attempt-store-uawrgcbe/version-accounting-diagnosis.json`。组合测试显式选择此运行时；未升级或改动开发环境正在使用的对象存储。

## 尚未完成

扫描是观察值，目录和远端列表并非跨文件一致性快照；并发写入仍可能在检查之后抢占额度。下一步需要事务性预留、实际增量结算与故障恢复，覆盖上传、复制、尝试封存和产物发布，再处理运行时文件增长和磁盘限制。

用户前缀之外的暂存、节点 S3 缓存、原生运行目录等宿主机空间，不能由本次用户存量扫描代表；需要单独的宿主机预算。既有调用持有的数据库事务/算例锁仍可能跨越慢扫描，移出事件循环并没有消除长事务和心跳争用。N4-05 的运行中未知消耗、提交后处理及全局并发治理也继续保留。

## 本批完成记录

组合验收进程62963成功退出：SQLite/PostgreSQL各本地8项、S3 9项，共34项。原输入封存兼容进程22263成功退出，两库各23项；真实S3作业兼容进程3833成功退出，14项。结果见 `tmp/mainline-storage-usage-evidence/validation.json`，临时数据库只读清理核对见同目录 `database-cleanup.json`。代码变更限配置、配额服务和新增存量计量服务；未变更数据库结构、开发服务和既有存储实例。

## 2026-11-20 S3 普通保存的用量盘点

普通 S3 保存此前在算例写事务及账户配额锁内列出实际对象和历史版本。新流程先取得账户配额版本和初始额度提示，结束数据库会话后扫描；写事务内重新确认算例与账户权限、当前套餐、全部有效预留及账户版本，然后提交本次对象写入预留。同一算例存储操作可复用初始额度提示来缩短盘点，实际额度仍在写事务内重新读取。收到其他写入或结算后必须重读，使用同一请求期限。已有预留在真实写入未证明结算前继续占额度。能提前证明已超额的盘点结果只作下界，不能用于批准写入。

- 基线：实际列出 18 条对象或历史版本、9,029 B 后暂停，SQLite 的独立写事务打不开，另一账户的算例修改未完成。释放后原保存成功。见 `tmp/storage-admission-inventory-dopvqc6e/storage-admission.json`。
- 修复：实际列出 36 条、18,089 B 后暂停，独立 SQL 写事务可打开，另一账户修改返回 200，同一账户另一算例保存返回 200 并读回。旧版本 6 遇到新版本 46 后重试，原保存随后返回 200，正文一致，两案 38 条物理写入预留均已结清。见 `tmp/storage-admission-inventory-dzw50m93/storage-admission.json`。专项不涉及模型，真实调用 0，模拟 0。
- 数据库增加 `storage_quota_epochs`，迁移 `339746ef01b2`。隔离 PostgreSQL 升级到了该版本；开发数据库及原服务未迁移或重启。已有数据库由原部署迁移流程先升级再切换代码。
- PostgreSQL + 认证 SeaweedFS 的普通保存和原文读回通过，真实 DeepSeek `deepseek-flash` 两次 HTTP 200，实际工具读回准确；模拟 0。隔离数据库已删除，S3 已停止。见 `tmp/live-cli-startup-yp2y6les/live-result.json` 和 `tmp/job-repeat-postgres-wv55ca5p/result.json`。另两轮默认 S3 配置的双 API 工况派生在模型调用前超过测试客户端的 30 秒期限，未计为通过，见 `tmp/live-cli-startup-hhlfskbg/live-result.json` 与 `tmp/live-cli-startup-gc9s_664/live-result.json`。改用认证 S3 后的首次尝试也在 30.02 秒处超时，见 `tmp/live-cli-startup-08kakny9/live-result.json`；这些尝试未发起模型推理。
- 减少重复查询后复跑，诊断客户端给原竞争断言最多 90 秒，未调整生产 API 期限。获胜请求实际 9.27 秒返回 200，另一 API 返回目标已存在的 409。四份派生输入摘要、原工况、作业列表和唯一文件刷新事件均核验通过。真实 DeepSeek 再调用两次，均为 HTTP 200，工具回读准确。见 `tmp/live-cli-startup-q5_8km84/derive-collision.json`、同目录 `live-result.json` 和 `tmp/job-repeat-postgres-bsn9gd7z/result.json`。隔离 S3 已停止，临时 PostgreSQL 已删除。

这次覆盖持有算例存储预约时的逐对象 S3 写入。整账户批量预留及文件系统准入仍有锁内盘点路径，RB-14 继续按原范围复核慢存储、截止时间和心跳组合。未关闭 N4-05、RB-12 或 RB-14。

## 2026-11-20 S3 批量存储额度

常规批量写入先在短事务中处理已存在的额度，再退出事务盘点。发放额度时重新核对账户版本、有效预留和当前套餐；发现旧盘点则只重试读入和申请，已有对象写入不会重放。宿主事务结算批量额度也会推进版本。带或不带幂等标识的 S3 上传创建共用已经预留的额度，不再在创建事务中重复盘点。后续上传正文仍单独持久预留，批量额度同时约束元数据和恢复日志。

- 基线：盘点实际 18 个记录、9,029 B 后暂停。SQLite 独立写事务不可开，另一账户的修改请求尚未完成；释放后上传会话创建并读回。证据 `tmp/storage-pool-inventory-160aqr9l/storage-admission.json`。
- 修复：盘点实际 36 个记录、18,089 B 后暂停，独立写事务可开，另一账户修改及同账户另一算例保存均返回 200。原盘点版本 6 遇到 46 后作废，重扫 44 条、21,367 B；本次创建共两次盘点，无第三次锁内盘点。批量预留 5 条、逐对象写入预留 19 条均结清，30 B 的上传承诺仍在。把测试账号套餐改为 1 MiB 后，1 MiB 的新上传被拒绝，返回 429 且没有新增上传记录或活跃批量额度。证据 `tmp/storage-pool-inventory-f_vmyi84/storage-admission.json`。两项专项只用实际基础设施，真实模型 0，模拟 0。

- PostgreSQL 与认证 SeaweedFS：旧式上传可创建和取消，带幂等标识的创建重放返回同一会话。实际写入 86,016 B 和 180,224 B 两块，完结后原文和 SHA-256 均一致。本组 `deepseek-flash` 两次 HTTP 200，工具读回准确；模拟 0。隔离数据库已删除。证据 `tmp/live-cli-startup-0nbf5evk/storage-pool-transfer.json`、同目录 `live-result.json`、`tmp/job-repeat-postgres-174wghzm/result.json`。
- 扩展浏览器兼容回归先暴露两处未通过：现有工作树的 `dist` 在初次检查目标后提交按钮未启用；已交付的 FF-H17 构建能断线重连并写完五个分块，但提交遇到账户配额锁返回 409。见 `tmp/live-cli-startup-5mmmle9o/browser-failure.json` 和 `tmp/live-cli-startup-xn_ibpya/browser-failure.json`，这两轮没有模型调用。普通 S3 上传提交现于进入算例事务前等待同一个账户锁，保留原期限和精确写入校验。
- 实际 PostgreSQL 账户锁竞争：在整账户额度已授予后，用另一连接持有 advisory lock 0.8 秒。上传提交等到锁释放才返回 200，20,480 B 原文及摘要通过核验。真实 DeepSeek 2 次 HTTP 200，模拟 0，临时数据库已删除。证据 `tmp/live-cli-startup-srr_ibns/upload-complete-owner-contention.json` 和同目录 `live-result.json`。复跑入口 `tmp/quota-upload-completion-contention.py`。
- 完整复跑通过：在已交付的 FF-H17 浏览器构建、隔离 PostgreSQL、认证 SeaweedFS 上，真实浏览器两次掉响应后续传同一会话，5 块共 17,825,805 B，实际读回摘要及多段下载均一致。真实 API 三次退出均恢复或取消成功，终态账户预留为 0。真实 DeepSeek 再调用 2 次，均 HTTP 200，回读匹配。详见 `tmp/live-cli-startup-7l1sjdf7/upload-creation.json`、同目录 `browser-upload.json` 与 `live-result.json`。此结果不把首轮 `platform/web/dist` 的未启用按钮计为已验收。
- 本地文件系统的当前基线：盘点已读取 29 条、4,227 B 后暂停，独立 SQLite 写事务不可开；释放后原文件保存并读回成功。此记录确认本地普通保存仍需处理。证据 `tmp/storage-fs-inventory-iom2mzhc/storage-admission.json`。

此次没有新增数据库迁移。分块上传转移额度、旧预留的对象核验及本地文件系统准入仍有原来的锁内路径。继续按 RB-14 检查原范围，当前任务未关闭。

## 2026-11-20 本地保存与冷恢复盘点

普通文件保存和 caseDict 准备完暂存后，先读账户版本并关闭数据库会话，再读取磁盘实际用量；取到写锁后拒用过期盘点、重新核对账户和全部预留。单次请求重试保持原期限。正常发布预先实际写入回执候选，计入账户峰值和节点暂存；只有查到匹配的 SQL 见证，才将候选换成正式回执，提交后无需为回执重新扫描账户。显式冷恢复和走同一入口的普通读取，恢复准备、正式回写前的两次盘点均移出写事务，目录状态、原文件、物理峰值及数据库见证仍逐项核对。

- 普通保存基线在实际磁盘盘点 29 条、4,227 B 后暂停，SQLite 独立写事务不可开。修复后 54 条、8,485 B 的暂停期间，另一账户 PATCH 和同账户另一算例保存均成功。原盘点版本 37 对比 39 作废，重扫 63 条、9,288 B 后保存并回读。两案的正式回执均已归档，标记请求合计两次盘点，没有提交后的第三次盘点。见 `tmp/storage-fs-inventory-iom2mzhc/storage-admission.json` 与 `tmp/storage-fs-inventory-a2imt12x/storage-admission.json`。
- 冷恢复在实际文件替换完成、SQL 提交前让隔离 API 以 84 退出。两轮重启均见 `commit-ready.json`，并按真实 SQL 回滚至原字节，归档 `rolled_back` 回执后不留候选。基线暂停 70 条、10,166 B 的实际盘点时，独立写事务不可开；修复后另一账户的修改和同账户另案保存可完成，版本 39 对比 41 后重扫并恢复成功。见 `tmp/storage-fs-cold-inventory-3qdrsx1y/storage-admission.json` 与 `tmp/storage-fs-cold-inventory-l0j9kkm6/storage-admission.json`。
- 已审查的文件系统诊断和 caseDict 专项通过，含 40 MiB 恢复期间另一算例写入 200、暂存和节点预留结清、七个编辑入口、无操作版本保持及过期版本 409。见 `tmp/local-quota-ordinary-restart-20261120.json` 与 `tmp/local-quota-dictionary-staged-20261120.json`。这些检查未使用模型。
- 临时 PostgreSQL 两轮 caseDict 真实 API 退出测试通过（普通保存与冷恢复修改后各跑一轮）。未提交时磁盘的三个字典关联文件可见，但数据库版本未前进；重启恢复三份旧摘要，再次提交推进唯一修订。实际 `deepseek-flash` 四次 HTTP 200，真实工具读回匹配；模拟 0。见 `tmp/live-cli-startup-4q3j9fts/live-result.json`、`tmp/live-cli-startup-otpqaunx/live-result.json` 与同目录 `dictionary-crash.json`，临时数据库已删除。

本批复用已有账户版本表，无新增迁移。遇到旧事务须在协调器内兼容恢复时，旧盘点会被作废，仍可触发原来的锁内额度检查。其他本地批量写入、S3 分块额度转移和整套超时、取消、心跳组合仍按原 RB-14 清单推进；RB-12 等任务状态保持。
