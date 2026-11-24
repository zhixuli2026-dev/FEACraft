# 工作区备份逐块编码与内存峰值

## 2026-11-19 单个大文件恢复

旧版 v1 ZIP 的文件数据现在按 JSON 字符串块解码。暂存只持有完整 Base64 组、文件位置、长度与 SHA-256。恢复创建算例时，使用现有带额度的文件复制与不可替换提交，分块读回暂存并再次核对摘要。编码格式、最终文件清单、账户和节点额度以及 caseDict 镜像验证沿用原协定。

SQLite 和文件存储通过正式 multipart 接口恢复同一个 40 MiB 原文件。整次请求的 Python 分配峰值为 17,551,104 B。正式下载的长度与 SHA-256 一致，恢复后节点暂存预留为 0。无效 Base64 返回 HTTP 400，节点额度不足返回 409，两次失败都没有新增工作区。旧 UTF-16 清单的 Unicode 文件名与转义 Base64 可读回。真实 `deepseek-flash` 通过一次文件读取工具核对归档内原文本，官方服务返回两次 HTTP 200，模拟 0，隔离 API 已核验退出。[文件存储回执](../../tmp/backup-large-entry-eoicoc73/backup-large-entry.json)

PostgreSQL 和真实 SeaweedFS 恢复并正式下载同一 40 MiB 文件，整次请求峰值为 3,344,192 B。已核验临时额度、无效 Base64、UTF-16、空文件和真实模型读取。官方 DeepSeek 返回两次 HTTP 200，模拟 0，API 和对象服务退出，临时数据库已删除。首轮 S3 的暂存文件指针在文件末尾，提交校验拒绝了内容不一致。共享写入入口在实际 S3 PUT 前回到起点，复验通过。[S3 回执](../../tmp/backup-large-entry-x95zb25i/backup-large-entry.json) · [临时数据库清理](../../tmp/terminal-agent-postgres-9bi_stjc/cleanup.json) · [首轮拒绝](../../tmp/backup-large-entry-xysclhzx/backup-large-entry.json)

文件系统上的 48 MiB 算例和工作区流式克隆兼容验收通过。[克隆回执](../../tmp/case-clone-streaming-8d4v32_i/clone-streaming-result.json)

用四种较短的读取边界，比较 20,000 个 Base64 片段与 Python 标准库的严格解码结果。测试包含 JSON 斜线、Unicode 转义、填充以及非法字符。完整验收入口为 [live_llm_backup_large_entry_acceptance.py](../server/live_llm_backup_large_entry_acceptance.py)。整次请求的 Python 分配峰值以 `tracemalloc` 测量。SQL 元数据及语义验证所需的 caseDict 文本仍按原处理。

## 2026-11-18 旧版 ZIP 恢复与同账户并发写入

工作区恢复现在按文件校验旧版 v1 清单，把内容写到临时文件，只将位置、长度与摘要留给后续写入。临时文件超过 1 MiB 时使用配置的本地存储根，按 4 MiB 递增申请节点空间。校验完所有文件、引用和 caseDict 后才申请账户写入额度，再检查配额并创建工作区。原有备份格式继续可用。

两套隔离服务通过正式 HTTP 入口导入同一份 12,710,372 字节 ZIP，内含两个算例、12 个各 1 MiB 的二进制文件、一个中文名空文件及两份 caseDict。15 个文件逐字节回读一致。下表是同一进程内原校验器和最终校验器的 Python 分配峰值。

| 数据库与存储 | 原校验峰值 | 最终校验峰值 | 临时空间预留 |
|---|---:|---:|---:|
| SQLite 与文件系统 | 67,181,864 B | 12,915,708 B | 16 MiB |
| PostgreSQL 与 S3 | 67,172,886 B | 12,905,405 B | 16 MiB |

测试在校验线程返回前暂停恢复，同一账户此时成功写入另一算例。损坏 ZIP、摘要错误及 caseDict 阶段冲突均返回 HTTP 400，节点空间不足返回 HTTP 409。逐次检查临时句柄均已关闭，节点预留归零，失败请求没有增加工作区。随后使用 UTF-16 编码的旧清单重试成功。[文件系统实测](../../tmp/live-cli-startup-20yrjxs0/backup-restore-memory.json) · [S3 实测](../../tmp/live-cli-startup-ulym1ydt/backup-restore-memory.json)

两套最终运行各有 2 次真实 DeepSeek HTTP 请求，模型通过工具回读本轮验收摘要。开发期间两轮对照另有 8 次真实请求。首版解析器在此样本只降低约 2% 的峰值，最终实现以标准库逐项解码并释放已处理的文本。[文件系统模型回执](../../tmp/live-cli-startup-20yrjxs0/live-result.json) · [S3 模型回执](../../tmp/live-cli-startup-ulym1ydt/live-result.json) · [首版测量](../../tmp/live-cli-startup-n9jzidqe/restore-peaks.json)

最大单个文件、元数据及 caseDict 仍会影响内存峰值。另用仅含一个 12 MiB 二进制文件及一份 caseDict 的 ZIP 做本地校验，文件摘要一致，峰值从 67,158,911 B 降至 62,932,477 B。此处统计校验阶段的 Python 分配。进程的总内存、通用配额和跨节点行为仍由 N4-05 跟踪。[单大文件测量](../../tmp/restore-single-measure-pyvjo7xl/memory.json)

## 2026-11-18 备份下载与断连回收

工作区备份在返回 HTTP 200 前完成源清单复核。压缩输出在 1 MiB 以内保存在内存；超过阈值时，先在配置的本地存储根申请节点空间，再创建随句柄关闭而删除的临时文件。预留随压缩包增长，以 4 MiB 为单位，并受原 100 MiB 上限约束。下载期间每次最多读取 64 KiB，数据库提交和工作区锁均在发送前完成。调用备份服务取得字节的已有调用者仍可取得完整 v1 包。

用同一清单和同一份 12 MiB 随机二进制文件分别执行内存打包与实际下载，两份 ZIP 的 SHA-256 相同。表中为 Python 打包阶段的峰值分配，节点预留是同一节点上同时存在的全部 ZIP 预留之和。

| 数据库 / 源存储 | 原内存输出峰值 | 下载打包峰值 | 实际 ZIP | 节点预留 |
|---|---:|---:|---:|---:|
| SQLite / 文件 | 19,755,760 B | 6,461,521 B | 12,711,492 B | 16 MiB |
| PostgreSQL / S3 | 19,442,987 B | 6,683,167 B | 12,711,557 B | 16 MiB |

两组均通过正式下载和恢复接口回读原文件。节点在已预留 4 MiB 后拒绝下一次增长，接口返回 HTTP 409 和 `node_storage_full`。隔离测试还分别降低压缩上限、在打包后实际增加源文件，都在发送 ZIP 前被拒绝。刷新下载能看到新文件。早期测试发现容量异常被归类为源文件变化，现已分别保留容量和源版本错误。[文件存储验收](../../tmp/live-cli-startup-9l9kg0j8/backup-download.json) · [S3 验收](../../tmp/live-cli-startup-3e3o8p52/backup-download.json)

实际中断下载后，节点预留归零。Windows 上强制终止正在发送的隔离 API 时，系统关闭并删除打开的 ZIP；第二个 API 复核旧进程已退出，清理 4 笔预留并完成下载。最初一次 S3 测试在客户端读超时，验收程序现保持部分响应的迭代器存活，并在读取竞争请求的正文前先验证其 429 状态。两组均验证重启后的原 API 可重新下载。两轮最终验收各有 2 次真实 DeepSeek 回读请求。[文件存储模型记录](../../tmp/live-cli-startup-9l9kg0j8/live-result.json) · [S3 模型记录](../../tmp/live-cli-startup-3e3o8p52/live-result.json)

这些数据覆盖 Windows API 使用文件或 S3 作为项目源。节点预算会保守地同时计入盘上已可见的字节和活动预留。其他长文件操作、完整 v1 恢复 JSON 的解析内存、SQL 元数据及 caseDict 解析继续由原资源工作清单跟踪。

## 2026-11-18 工作区恢复内存

恢复接口直接从 multipart 上传已经使用的临时文件读取 ZIP。校验器先测量该文件的实际长度，验证摘要后释放 Base64 字段，使用已解码的 caseDict 核对数据库镜像。用于写入恢复工作区的文件只保留一份解码内容。原有调用者通过字节读取 v1 清单时仍可取得编码字段。

同一份含 12 MiB 随机二进制文件的实际 v1 包，新旧加载路径逐文件摘要相同。使用 Python `tracemalloc` 测得校验结束后仍持有的内存从 42,167,307 字节降至 12,608,901 字节，校验峰值从 75,718,308 降至 60,307,912 字节。测量涵盖读取上传包至完成校验；ZIP 内完整 JSON 解析和后续数据库写入的资源边界仍需跟踪。[原始测量](../../tmp/live-cli-startup-kyzwj4h0/restore-memory.json)

正式 multipart 接口已通过文件存储/PostgreSQL 和 S3/PostgreSQL 复验。恢复到独立工作区后下载二进制文件，逐字节与原件一致；摘要错误返回 400，超过 100 MiB 上限一字节返回 413，两种拒绝都未创建工作区。离线包格式回归通过 21 项，包括内存和落盘两种上传文件。两轮双后端联调中的 DeepSeek 共 8 次请求，用于验收摘要回读。[文件存储](../../tmp/live-cli-startup-_kq4dmx3/workspace-restore.json) · [S3](../../tmp/live-cli-startup-7ll_vgi4/workspace-restore.json) · [格式回归](../../tmp/vibeflow-regression-all-20261118-104858-6c9696/backup_manifest_smoke.log)

2026-11-17，N4-05 / RB-12、RB-14 的实际实现增量。主线 49/53、剩 4 项；资源清单 11/17、剩 6 块；295 项全功能范围保持。Claude 已停用，本阶段真实模型 0、模拟 LLM 0。

## 改动

原 v1 备份先读取全部文件，将每份 Base64 字符串放进 `manifest.files`，再整体验证并写 ZIP。即使最终 ZIP 很小，文件正文和 Base64 列表仍会同时驻留内存。

现在 `backup._pack_backup` 只保留文件元数据，直接把 Base64 字段逐块写入 ZIP 的 `manifest.json`。编码器跨块只保留至多 2 个未编码字节；FS 以最多 1 MiB 的版本绑定范围读取，S3 复用真实条件请求和逐块 snapshot。两者均验证实际大小和完整 SHA-256。未改变 v1 schema、文件内容、下载名称、恢复接口或原请求准入规则。

`backup_manifest` 拆出结构/关联/路径验证和 caseDict 语义验证，导出与导入共用。恢复仍检查 Base64、文件总大小和摘要，仍在创建工作区之前拒绝无效包。caseDict 仍需要读取正文进行解析；导出结束后的整个源清单复核、数据库事务及取消排空继续保留。源在捕获后变化时返回 409，不输出成功 ZIP。

## 实测

同一隔离 API、同一真实文件集分别执行改动前的实际生产打包函数和新函数，使用 `tracemalloc` 分别记录打包阶段峰值分配。基线源码来自 `tmp/backup-streaming-before-20261117.zip`，不是模拟输出。文件集包含约 24 MiB 大二进制文件、约 1 MiB 二进制文件、空文件、1/2 字节文件、中文/emoji 路径和原算例脚手架，共 18 个文件。解压后的新旧 manifest 完全相同。

| 后端 | 原峰值字节 | 新峰值字节 | 实际结果 |
| --- | ---: | ---: | --- |
| FS / SQLite | 102,924,161 | 5,740,248 | `tmp/backup-streaming-fs-64soqz3b/backup-streaming-result.json` |
| S3 / SQLite | 103,197,272 | 5,787,246 | `tmp/backup-streaming-s3-1rxa4kiz/backup-streaming-result.json` |

两组均约降低 94%。这是给定文件集的 Python 跟踪分配峰值，不是 RSS、操作系统硬限额或任意备份大小下的固定内存保证。

每组 10 项实际检查：新旧 manifest 相同且峰值下降；二进制/边界/路径逐字节正确；新包经公开恢复接口实际还原并读回；旧包仍可恢复；错误摘要、错误 Base64、越界路径、缺 caseDict、数据库镜像不一致分别返回 400 且不创建工作区；捕获后真实源变更返回 409。两次 API 均正常关闭并按出生身份独立确认退出，S3 服务停止。

既有 `archive_capacity_checks.py` 在复核导入辅助代码并更新源码审查登记后，仅执行 FS/SQLite 和 S3/SQLite 两组：`tmp/archive-capacity-j28cyrty/checks.json` 18 项、`tmp/archive-capacity-q5i8t11c/checks.json` 20 项。覆盖实际 ZIP 导入/恢复、源/manifest/ZIP 三个上限、节点/所有者额度拒绝及真实压缩/解压线程取消排空、暂存和预留结算。两个隔离 API 由原测试辅助程序等待退出；本轮未运行 PostgreSQL 分支或任何历史模拟模型集合。

合计 58 项检查，均属于实际文件、HTTP、存储或明确故障测试，不计为模型验收。汇总和逐文件源码归档位于 `tmp/mainline-N4-05-backup-streaming-evidence/`。

## 原范围与后续

此次移除的是导出过程的整工作区正文/Base64 驻留。最终 ZIP 仍受原 100 MiB 上限约束并保存在内存；SQL 元数据、caseDict 解析和恢复时的 v1 JSON/解码文件仍有内存占用。现有同节点单请求准入、300 秒期限和响应分块继续有效，见 [跨 worker 准入](archive-cross-worker-admission.md)。本次未宣称全入口内存预算、多节点全局额度或 OS/WSL 硬边界已完成，原 RB-12 至 RB-17 继续推进。

## 2026-11-19 备份打包时的写锁

RB-14 的正式工作区备份先按原工作区与算例锁核对权限、活动作业、成员、文件事件序号及 v1 所需的历史数据。纯读扫描与 ZIP 打包在该事务和原 S3 所有者写入额度释放后执行。发送前再次协调工作区，检查原权限、成员、事件和历史记录，加上最终目录与所选文件的实际版本。FS 版本绑定实际文件身份，S3 版本检查使用逻辑与物理对象约束。原 SHA-256 核验、节点 ZIP 暂存、v1 限制与正式恢复接口保持。[生产实现](../server/app/services/backup.py) · [新验收入口](../server/workspace_backup_lock_smoke.py)

旧版真实 SQLite/文件系统在正式备份读取第一个 1 MiB 文件块后暂停。独立写连接无法取得 `BEGIN IMMEDIATE`，同算例正式 SHA 条件保存返回 `409 storage_quota_busy`。继续打包后，42,364,824 B 的正式 ZIP 返回 200，源 40 MiB、真实 Windows 作业历史及空会话保持原始数据。同一保存参数重试成功，随后正式恢复并读回原 SHA-256，节点预留归零。精确旧版回归 1/1，API 的创建句柄退出和监听关闭均已核验。[旧版实测](../../tmp/workspace-backup-lock-fs-m_yitha3/workspace-backup-lock.json) · [执行摘要](../../tmp/vibeflow-regression-all-20261119-172701-d59cd5/summary.json) · [独立退出](../../tmp/workspace-backup-lock-fs-m_yitha3/process-exit-independent.json)

首次固定轮已在文件系统实测原保存 200、独立写连接成功、冲突出包被拒绝。旧夹具在紧接 409 的重试断言中丢失了具体 HTTP 状态，执行器停止在正式恢复之前。修订夹具记录完整状态，仅对已有 `429 archive_busy` 按其 `Retry-After` 重试。该轮失败和退出结果保留。[首次固定记录](../../tmp/workspace-backup-lock-fs-_aix1cpp/workspace-backup-lock.json) · [独立退出](../../tmp/workspace-backup-lock-fs-_aix1cpp/process-exit-independent.json)

最终文件系统和认证 SeaweedFS 均使用实际 SQLite。两边的正式备份暂停在真实 40 MiB 文件的首块读取时，独立 SQLite 写连接能取锁，同算例按 SHA 条件保存返回 200，原备份返回 409。在打包和第二轮摘要结束后、最后版本复核前，从实际文件系统或认证 S3 加入目录成员，正式备份均返回 `409 备份期间源文件清单已变化`。两个冲突用例均终止 ZIP 响应。[文件系统结果](../../tmp/workspace-backup-lock-fs-w7dl4evp/workspace-backup-lock.json) · [SeaweedFS 结果](../../tmp/workspace-backup-lock-s3-neyqzg8h/workspace-backup-lock.json)

两个后端紧接最近 409 的第一次重试都实际收到 `429 archive_busy` 和 `Retry-After: 2`。清理后节点预留为零，随后返回 42,365,031 B 和 42,365,119 B 的 ZIP。两个 ZIP 含最新保存和新目录成员，经正式恢复接口逐块读回原 40 MiB 的 SHA-256。只读 SQLite 独立核对旧真实 Windows 作业历史、原生停止证明、现有会话和活动所有者额度 0。跨所有者均返回 404。[文件系统恢复历史](../../tmp/workspace-backup-lock-fs-w7dl4evp/independent-restored-history.json) · [SeaweedFS 恢复历史](../../tmp/workspace-backup-lock-s3-neyqzg8h/independent-restored-history.json)

最终精确回归 1/1，耗时 156.25 秒，真实模型及模拟调用均为 0。两个隔离 API 和认证 SeaweedFS 的原进程均经独立核验退出。C 盘在固定轮前后可用 21.56 GiB 和 21.55 GiB，D 盘 382.16 GiB 和 381.26 GiB，11 个项目 Docker 容器继续运行。本轮范围是正式工作区 ZIP 导出从纯读打包到提交前版本复核，真实恢复用于检查 v1 兼容。最终版本列表和元数据仍在原协调边界内复核。[执行摘要](../../tmp/vibeflow-regression-all-20261119-173444-b26650/summary.json) · [文件系统退出](../../tmp/workspace-backup-lock-fs-w7dl4evp/process-exit-independent.json) · [SeaweedFS 退出](../../tmp/workspace-backup-lock-s3-neyqzg8h/process-exit-independent.json)
