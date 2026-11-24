# RB-12 部分验收：staging 下载与本地创建的节点峰值

## 2026-11-21 下载失败和进程退出后的本地暂存回收

S3 下载准入覆盖正文、旧版清单和准备记录的写入峰值。开始下载前落盘记录 API 进程身份和本地暂存目录标识。下载结束、清单写入后移除准备记录，再把目录交给调用方。准备记录存在期间，同步入口拒绝读取该副本。

失败回滚逐项删除并沿用现有请求期限。当前期限耗尽或文件被 Windows 句柄占用时，原始请求错误继续返回，后台维护根据已落盘的弃用状态接续删除。API 实际退出时，后台通过原进程身份验证退出后回收其未交接暂存。退回节点预留后，磁盘上仍存在的文件继续计入物理用量。

SQLite 与 PostgreSQL 均使用认证 SeaweedFS。实测暂停了已写入的 300 KiB 文件，直接触发下载期限和 Windows 文件占用；运行期间实际节点预留为 322,233 B，失败后预留归零。现场文件仍计入物理用量，续删完成后回到基线。实际 API 退出后，新 API 核对原进程、完成回收并从同一远端重新准备成功。每库 11 项实际基础设施或故障检查，模型调用 0。[SQLite](../../tmp/staging-sync-6opq5dpu/staging-transfer-deadline.json) · [PostgreSQL](../../tmp/staging-sync-9_2jez6s/staging-transfer-deadline.json) · [临时数据库](../../tmp/terminal-agent-postgres-xixzna4w/cleanup.json)

PostgreSQL、非特权 WSL 和认证 SeaweedFS 的真实 DeepSeek CLI 恢复回归另通过 8 项。两个真正的 `deepseek-flash` 回合覆盖实际审批、进程退出及恢复后读取本地文件，模拟调用 0。CLI 上游 HTTP 次数未逐笔采集。[CLI 结果](../../tmp/live-cli-staging-recovery-bmu5tpcy/live-result.json) · [CLI 临时数据库](../../tmp/cli-staging-recovery-postgres-6ywiwgvx/result.json)

## 2026-11-20 通用类型作业的 S3 输入校验

通用类型作业提交与发布会校验整案文件。原 S3 实现以单次读取接收每个对象的完整正文，再写入节点临时副本。现在复用目录版本和对象条件读取，以最多 1 MiB 一块计算实际 SHA-256。校验前后复核原文件清单和版本。旧 S3 前缀沿用原条件读取，空对象保持同一版本检查。Windows 的 `.CMD` 等扩展名保留原可执行位判定，专业求解的 S3 来源继续使用其原有权限语义。实际作业封存仍通过原有节点和远端准入。

| 验收 | 实测 | 证据 |
| --- | --- | --- |
| 修复前 | 真实认证 SeaweedFS 上，同一份 24 MiB 输入在提交和发布各整块读取一次。每次额外生成 25,168,290 B 的节点校验副本。 | [基线](../../tmp/live-cli-startup-w_fso672/typed-command-source-result.json) |
| 修复后 | SQLite、PostgreSQL 各执行真实 Windows 类型作业，提交和发布均以 1 MiB 一块核验 24 MiB 对象。两次校验的本地新增副本和写入次数均为 0。发布文件中的 SHA-256 与实际输入一致，同键重放返回原作业。 | [SQLite](../../tmp/live-cli-startup-9fbu2mvd/typed-command-source-result.json) · [PostgreSQL](../../tmp/live-cli-startup-zok94508/typed-command-source-result.json) |
| 版本与兼容 | 两组均验证空文件、Windows `.CMD` 可执行位、专业求解共享读取、未建立目录的旧 S3 前缀。旧前缀读取暂停时实际替换同长对象，原请求返回 409 并重新核对，后续请求读到新 SHA-256。 | [SQLite 读验收](../../tmp/live-cli-startup-9fbu2mvd/typed-command-source-result.json) · [PostgreSQL 读验收](../../tmp/live-cli-startup-zok94508/typed-command-source-result.json) |
| 本地文件系统 | 正常类型作业通过，实际执行、封存内容、发布和同键重放通过。 | [本地回归](../../tmp/live-cli-startup-r767jzxz/typed-command-source-result.json) |
| 专业求解历史兼容 | 在隔离 PostgreSQL 和恢复的真实 SeaweedFS 上读取原 UDF 历史发布，实际新引用作业核验当前来源。Windows 真实缓存句柄释放后完成清理，第二个引用作业通过。 | [历史发布引用](../../tmp/live-cli-startup-jvmiqd48/original-udf-reference.json) |

验收范围是输入校验的副本峰值与正式作业兼容。提交前正文摘要读取的锁范围已在后续阶段单独实测。启动前的第一个对照因验收探针对 Botocore 参数名兼容错误而失败，修正后取得真实基线。[提交前读取](submission-capacity.md#2026-11-20-s3-类型作业的提交前读取) · [首次记录](../../tmp/live-cli-startup-bmdhzehl/live-result.json) · [实现](../server/app/services/s3_case_files.py) · [验收入口](../server/live_llm_case_command_source_acceptance.py)

真实 `deepseek-flash` 回读共 14 次，全部 HTTP 200，模拟 0 次。历史 UDF 夹具首轮缺少它要求的 PostgreSQL 参数，改用隔离 PostgreSQL 后通过。阶段前后 C 盘可用空间均为 22.03 GiB，Docker 构建缓存为 22.67 MB，Docker 回收与 Windows 实际释放均为 0。[模型回读](../../tmp/live-cli-startup-jvmiqd48/live-result.json) · [历史夹具首轮记录](../../tmp/live-cli-startup-gl_cy18k/live-result.json)

后续更新：S3 创建、克隆与恢复的账户持久预留已在[后续阶段](owner-storage-admission.md)实现并验证。下文保留本阶段边界；当前剩余工作以资源工作清单及主线台账为准。

2026-11-06：本次完成两个共用写入入口的节点准入，不代表整个 RB-12 完成。主线仍为 33/53 完成、剩余 20 项；资源预算阶段仍为 11/17 完成、剩余 6 块。

## staging 下载

`storage.prepare_job_staging` 统一调用 `staging_download.prepare`。先列出非保留路径，验证规范文件名、大小、版本和路径冲突，计算文件正文加完整恢复 manifest 的字节数。节点额度不足或现有占用无法核验时，不创建 staging 目录、不读取文件正文。

取得节点预留后，按清单中的 ETag 条件读取，每次最多 1 MiB；不再把每个求解器输出完整装进内存。返回长度、版本、实际读取长度均须匹配。下载结束后重新检查来源清单，来源变动则清理本次未交付副本。manifest 的摘要字段预先使用同长度占位符，因此最终编码大小必须与预留一致。

预留贯穿整个下载、manifest 写入和异常清理。取消等待者不能提前释放仍在工作的线程所占额度。写入者退出后，下一次节点准入可依据进程出生身份释放其未来预留，但残留的部分文件继续计入实际用量，不自动删除。

该入口供旧 Job、终端、可视化、Agent shell 和 Codex 会话调用。本次还将 Job 准备阶段的节点不足、用量不可核验错误归类为 `resource_limit`，保留 `disk_limit`/`disk_unverified` 预算证据，避免误报成普通进程启动失败。

## 本地算例创建与克隆

FS `case_provision.put_new` 在写入前预留 `2 × 内容字节数 + 实际安装意图记录字节数`。临时文件与最终文件在硬链接安装期间有两个名称，现有节点盘点会分别计数，因此预留覆盖该保守峰值。文件安装、同步以及临时链接和意图记录清理都在同一个预留范围内。

该入口同时用于算例文件、数字前缀占用记录、待提交创建计划和恢复历史。每次文件安装独立准入；它不承诺整个克隆一定能完成。中途额度不足时，既有创建恢复协议保留或回滚对应现场；已写入内容继续计费。本地用户侧创建/恢复元数据预算已在[后续阶段](local-provision-storage-admission.md)补齐，S3 部分仍待完成。

## 验收证据

本次共 55 项不同场景：staging 底层 10 项、真实 Job 准入 2 项、FS 创建/克隆节点 6 项、旧 staging 合并 5 项、终端双数据库 18 项、创建恢复双数据库 8 项、硬链接安装中断恢复双数据库 6 项。克隆并发场景另以更严格的对照重新验证：为竞争者自身的预留记录留出空间，复制期间拒绝，原写入结算后同样大小可成功准入；该复核不重复计入 55 项。

staging 底层 10 项先独立完成，后续集成夹具曾因与人工构造的 owner 1 共用 S3 命名空间而触发无关用户额度。现将底层夹具改用 owner 9001，并在全新对象存储重跑了两个 Job 场景；没有把原失败套件整体计为通过。

- [阶段验收](../../tmp/mainline-staging-provision-node-evidence/validation.json)
- [源码绑定](../../tmp/mainline-staging-provision-node-evidence/source-binding.json)
- [资源预算工作清单](resource-budget-worklist.md)

## 原范围的后续验收

1. 本地创建、克隆与恢复元数据已有用户空间预算验收。S3 对应入口使用持久账户预留，已有算例及工作区复制的真实 40 MiB、提交见证和冷恢复证据。[本地创建](local-provision-storage-admission.md) · [S3 账户预算](owner-storage-admission.md) · [复制实测](constructor-recovery.md#2026-11-20-s3-复制的最终准备与提交)
2. S3 staging 远端同步及恢复使用逐笔持久预留。普通 S3 保存、上传和删除已通过 40 MiB 实际前像和物理预约回归。CLI 暂存恢复另有非特权 WSL、PostgreSQL 和认证 SeaweedFS 的正式流程。[远端 staging](remote-write-storage-admission.md) · [普通文件事务](file-transaction-diagnostics.md#认证-s3-普通文件保存) · [CLI 暂存](cli-s3-staging-recovery.md)
3. 认证 S3 的算例和工作区复制在编号绑定前与提交见证前实际结束 API，冷启动能读取待决清单并按原版本清理，同名重试读回原摘要。超时时在途读取排空后返回 504。无法核验的远端写入按原持久预留继续对账。[实际退出与排空](../../tmp/case-copy-final-s3-097cuvzs/final-copy.json) · [未知预留对账](remote-write-storage-admission.md#2026-11-19-算例-s3-预留对账轮转)
4. RB-12 继续按原计划核对其他长文件及副本入口的峰值。长 I/O 的锁、期限和心跳由 RB-14 跟踪。当前整项状态见资源清单。[资源工作清单](resource-budget-worklist.md)
