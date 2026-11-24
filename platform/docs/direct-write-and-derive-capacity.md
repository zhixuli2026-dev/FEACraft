# 直接文件写入与工况派生

## 2026-11-20 文件系统冷启动恢复

原文件系统派生在原子重命名后、SQL 提交前退出。新进程从普通文件树返回了未提交的 40 MiB 目标，同名请求得到 409。当前派生复用目录事务，暂存文件和清单先持久化，数据库写入文件事务见证及目录刷新事件。冷启动从磁盘检查目录和流式 SHA-256，以提交见证决定保留目标还是移出公开目录。提交前退出不产生文件事件，同名可重试。提交后退出保留目标原摘要和一条文件事件，同名返回 409。[原版对照](../../tmp/derive-crash-fs-07v862i3/derive-crash.json) · [验收入口](../server/run_derive_recovery_acceptance.py)

目录清单沿用已有节点暂存准入和来源目录枚举期限。派生记录绑定一个 `C-run/<name>`，只记录文件总字节数与目录内容摘要。准备、正文复制和 SHA-256 在数据库锁外完成，安装使用同卷无覆盖移动。成功时沿用原文件树全量刷新事件，普通目录事务的既有格式与大小限制继续适用。回滚副本与回执进入已有的私有目录事务历史，保留恢复证据，计入实际磁盘占用。

Windows 源文件在最终散列后被覆写，且大小和修改时间恢复时，旧实现仍返回 200 并发布过期内容。固定版通过 NTFS 实际变更时间复核源文件，返回 409 并保留源修改。结果时刻、并行结果和后处理目录在首次枚举时直接跳过其子树。现有文件名大小写兼容、40 MiB 并发保存和超时排空均复验。[旧版复现](../../tmp/run-derive-final-fs-xnwuu7yy/derive-final.json) · [固定版](../../tmp/run-derive-final-fs-ojftbzr0/derive-final.json)

SQLite 复制 41,943,040 字节，PostgreSQL 复制 146,800,640 字节，两组分别在提交前以 86、提交后以 87 退出，记录旧进程与新进程 ID。恢复回执、同名重试、文件内容和事件数量均符合上述合同。测试使用临时数据库，PostgreSQL 已删除。Edge 双页中从实际 caseDict 选择基准、填写名称并派生成功，第二页无需刷新即可选到新工况，重名显示 409。第一次浏览器验收在现有虚拟环境缺少 Playwright，部署到 D 盘的验收临时目录后复跑。首次 5 秒界面等待在实际响应前到期，复跑观察到实际 HTTP 响应与两页状态。[SQLite 与 Edge](../../tmp/derive-crash-fs-p9l49qs2/derive-crash.json) · [PostgreSQL](../../tmp/derive-crash-fs-yz1pgdce/derive-crash.json) · [临时库删除](../../tmp/terminal-agent-postgres-3ctd2abv/cleanup.json) · [目录事务回归](../../tmp/sandbox-recovery-ui-retirement-c2dxm6gu/local-tree-proof.json)

文件系统和认证 SeaweedFS 各用两个实际 API 进程竞争同名目标。赢家发布四份完整输入，失败请求收到目录已存在的 409，双方原件和作业列表保持完整。过滤后只统计目标目录的文件刷新事件，每个后端恰好一条。认证 S3 的预约开始和结束记录与文件变更分开核对，活跃写入仍使用既有存储预约隔离。真实 DeepSeek `deepseek-flash` 两个双 API 场景共四次 HTTP 200，较早的单 API 复验另两次 HTTP 200，模拟模型为零。此前真实 icoFoam 的双工况计算和重跑结果继续作为工程产物证据。[文件系统竞争](../../tmp/live-cli-startup-xtybbjbe/derive-collision.json) · [认证 S3 竞争](../../tmp/live-cli-startup-0wpgql9e/derive-collision.json) · [真实求解](same-case-serial-batches.md)

部署无需迁移 SQL。回退前先暂停派生写入，通过现有文件事务诊断与恢复入口结清新版未决日志，再恢复旧 API。若访问实时目录时发现目录副本无法与日志签名匹配，保留目录和日志供核对。

## 2026-11-20 S3 最终复制与回滚

工况派生在取得算例的持久写入凭证后，使用已有 S3 文件事务准备并条件写入对象。大对象读取、上传、最终全目录复核及失败后的恢复都在长 SQL 事务之外。持久凭证禁止同算例的其他内容写入，其他算例可以正常保存。最终 SQL 只提交事务见证和文件事件，原来支持的文件模式记录仍随文件事务一起校验。派生超时停止继续发布，已有写入排空后由结算范围核对并回滚。

SQLite 与认证 SeaweedFS 的对照使用真实 40 MiB 文件。旧版持有 SQL 写锁，另一个算例在四秒观测窗口内未返回。新版可开启独立写事务，另一个算例保存 200，同算例返回 `409 file_sync_busy`。远端目标已写入后直接改变同尺寸源内容，派生返回 409 并回滚，源改动保留，按原名称重试的 40 MiB 文件摘要一致。小工况的共享期限耗尽后等真实读取排空，返回 504，回滚后可同名重试。节点预留和未结算的物理写额度归零。[旧版对照](../../tmp/run-derive-s3-final-4nu4ofxg/derive-s3-final.json) · [锁范围和回滚](../../tmp/run-derive-s3-final-nxy_67uu/derive-s3-final.json)

实际写入最后一个目标对象、尚无 SQL 见证时，让隔离 API 以退出码 86 退出。冷启动后使用正式文件事务恢复入口读回 `rolled_back` 回执，物理目标已清除，同名重新派生成功。前两次退出夹具分别遇到 Windows 启动器和连接重置的观测差异，保存其失败记录。最终仍独立核验退出码与旧进程出生身份。[实际退出与恢复](../../tmp/run-derive-s3-final-nxy_67uu/derive-s3-final.json) · [失败夹具一](../../tmp/run-derive-s3-final-z6kckbs2/derive-s3-final.json) · [失败夹具二](../../tmp/run-derive-s3-final-4za82bip/derive-s3-final.json)

当前版本再次通过实际并发、源修改回滚、超时后排空和进程退出恢复。并行首轮夹具在 10 秒内未到达预定暂停点，最后一轮将夹具期限设为 20 秒并在实际复制完成后的复核点等待过期。应用仍使用原默认期限。API 冷启动后同名派生成功，节点预留为 0，测试 SeaweedFS 退出码为 0。[最终复跑](../../tmp/run-derive-s3-final-ua4_6a1l/derive-s3-final.json) · [首轮观测](../../tmp/run-derive-s3-final-69wrps8b/derive-s3-final.json)

PostgreSQL 与认证 SeaweedFS 的兼容检查九项通过，包括空间不足拒绝、旧目标保护、已应用后的未提交回滚、共享成员按工作区所有者计费，以及物理写入票据全部结清。直接下载本次派生输入，非特权 WSL 实际运行 `blockMesh`、`checkMesh` 和 `laplacianFoam`，生成温度场。首轮用 WSL 登录 shell 时 `nobody` 未能启动，最后明确执行非特权 Bash 后完整复跑。临时 PostgreSQL 已删除。[PostgreSQL 结果](../../tmp/run-derive-capacity-yeekg0nq/checks.json) · [临时库删除](../../tmp/mixedread-postgres-o7nhv0nd/cleanup.json) · [首轮环境失败](../../tmp/run-derive-capacity-6p1g1gvf/checks.json)

文件系统和认证 SeaweedFS 的首次源扫描在本版复验通过。共用 S3 暂存事务的实际 Windows API、非特权 WSL、认证 SeaweedFS 与 SQLite 兼容验收通过八项，覆盖 API 退出、保留副本、防冲突、恢复及再次运行。观测到两轮真实 DeepSeek `deepseek-flash` CLI 模型回合，模拟 0，未逐笔统计上游 HTTP。派生实测未调用模型。[文件系统首扫](../../tmp/run-derive-source-fs-ilqa434z/run-derive-source-lock.json) · [SeaweedFS 首扫](../../tmp/run-derive-source-s3-b01lb_7_/run-derive-source-lock.json) · [真实模型及暂存](../../tmp/live-cli-staging-recovery-iqf79c6u/live-result.json)

## 2026-11-20 文件系统最终复制与提交

在正式 `POST /cases/{case_id}/runs:derive` 中，文件系统先核对源版本和所有者空间，再持有节点预留把输入复制到私有目录。复制、逐块摘要和源内容再次校验都在算例与账户数据库锁外。提交时重新确认权限、目标、算例事件序号、源文件清单与版本及所有者空间，随后无覆盖地原子发布目录。节点预留一直保留到发布或清理结束。不同卷的算例继续使用本卷私有目录，并按已落盘占用核对所有者空间。

`VIBEFLOW_RUN_DERIVE_TIMEOUT_SECONDS` 默认 300 秒，覆盖派生的授权、准入、复制、核对和提交。读线程共用该截止时间，超时先等已有读操作排空，清除尚未安装的暂存目录，再返回 `504 run_derive_timeout`。已安装目录由事务见证恢复，见本页文件系统冷启动恢复。

Windows 文件系统与 SQLite 的实际对照均使用 41,943,040 字节文件。旧版复制期间独立 SQLite 写事务无法开始，同案保存 409，旧内容派生 200。修复后独立写事务可开始，同案保存 200，旧派生 409。另在最终摘要完成后直接改写同尺寸源文件，旧派生仍返回 409。两秒期限触发后等实际文件读取排空才返回 504。三个拒绝场景均无可见目标目录或文件事件。最后按原名称重试，3 个文件发布，40 MiB 原件 SHA-256 与回读一致，再次提交返回 409。Windows 继续接受 `System/CONTROLDICT` 并保持实际文件名。节点预留回到 0 字节，私有暂存为空。真实模型调用 0，模拟 0。[修复前](../../tmp/run-derive-final-fs-3mne8a0s/derive-final.json) · [修复后五项](../../tmp/run-derive-final-fs-irsldxbi/derive-final.json) · [隔离进程与端口退出](../../tmp/run-derive-final-fs-irsldxbi/exit-proof.json)

原首次源扫描行为已在 Windows 文件系统和非特权 WSL 运行的认证 SeaweedFS 复验。同案保存 200，旧请求 409，按原名重试 200，原件与目标 SHA-256 一致，节点预留归零。首轮对照夹具的算例名含路径分隔符，被正式创建 API 拒绝，改名后才运行实际复制。[文件系统兼容](../../tmp/run-derive-source-fs-bzdl_i5h/run-derive-source-lock.json) · [SeaweedFS 兼容](../../tmp/run-derive-source-s3-u5rshjgb/run-derive-source-lock.json) · [最终源码审查](../../tmp/derive-final-copy-review-20261120-v2.json)

S3 的最终事务复制与整目录复核见本页 S3 最终复制与回滚。文件系统冷恢复见本页开头。

2026-11-06。N4-05 / RB-12 的增量交付，同时补齐 FF-C11 的 S3 派生入口。主线仍为 33/53 完成、剩余 20 项；资源清单 11/17 完成、剩余 6 块。本文不将测试数量计为任务完成数量。

## 实现

场景 WebSocket、网格质量评估、STEP 修复、Gmsh 预览和 UDF 脚手架在算例协调前取得工作区所有者的 S3 写入额度。额度覆盖实际文件事务、SQL 提交和已提交日志清理；提交与清理等待结束后才处理取消。FS 保留既有文件事务和节点峰值准入。

场景 WebSocket 将额度不足返回为 `storage_capacity` / 429，保持连接供用户重试；权限失效仍关闭连接。旧实现把所有 HTTP 异常都显示为权限失效，会掩盖存储额度问题。

`POST /cases/{case_id}/runs:derive` 现在支持 S3。它读取基准目录的大小/哈希清单，通过文件事务冻结并发布新目录，提交前复核源清单。源内容变化拒绝提交；目标存在时拒绝覆盖；未提交的已应用文件通过既有 SQL 见证日志回滚恢复。复制保留初始 `0/`，跳过其他数字时刻（含负数及指数）、`processorN`、日志、`.foam` 和基准内 `.vibeflow`。

新增 `s3_file_transactions.RemoteSource` 只引用同一算例中的源文件。每个文件使用大小受限、版本条件读取，校验清单哈希后上传不可变副本；中转文件位于受计量的存储根目录，写入前持有节点空间预留，使用流式复制。沿用普通文件事务限制：最多 256 文件、单文件 1 GiB、含备份合计 2 GiB。

FS 派生补齐整次复制的节点空间预留，源文件超过核定大小时在写出超额内容前拒绝；共享成员按工作区所有者检查额度。FS 原有原子目录发布方式保留。

## 实际验收

本批没有 LLM 调用，也没有模拟模型回复。测试入口为 `direct_write_capacity_checks.py` 与 `run_derive_capacity_checks.py`。已审查导入的隔离 API、真实 S3、临时 PostgreSQL、纯 OpenFOAM 输入辅助函数；仅调用迁移辅助函数，不执行旧回归集合。测试用故障注入控制实际文件写入后的异常和取消，不替代模型或工程软件输出。

| 验收组 | SQLite | PostgreSQL | 检查数 |
| --- | --- | --- | --- |
| 直接写入 / S3 | `tmp/direct-write-capacity-2w0hwxky/checks.json` | `tmp/direct-write-capacity-9k0wjyg_/checks.json` | 17 + 17 |
| 直接写入 / FS | `tmp/direct-write-capacity-213_1e3m/checks.json` | `tmp/direct-write-capacity-v21ga8op/checks.json` | 12 + 12 |
| 工况派生 / S3 | `tmp/run-derive-capacity-ax76ytdj/checks.json` | `tmp/run-derive-capacity-9c6scfda/checks.json` | 9 + 9 |
| 工况派生 / FS | `tmp/run-derive-capacity-74foszqa/checks.json` | `tmp/run-derive-capacity-_ppgc8yc/checks.json` | 7 + 7 |

以上共 90 条检查。新增远端复制类型后，重新运行了 S3/SQLite 直接写入组；此前 `direct-write-capacity-jxg312pu` 的同组 17 条保留为历史证据，不重复计数。其余直接写入组覆盖本次同一入口实现；后续增加的复制分支由派生组验证。

直接写入覆盖实际 WebSocket 保存/清空/哈希冲突、S3 五个写入口零额度拒绝且物理用量不增长、9 个 UDF 文件、真实 blockMesh/checkMesh 日志与质量报告、CadQuery STEP 放大后体积 48、meshio/VTK 四点单四面体往返、无效/缺失输入、真实未提交写入故障后的恢复重试，以及提交后取消时等待清理再释放额度。

派生覆盖真实文件字节与过滤规则、已存在/部分存在目标保护、非法名称与源目标重叠、账户零额度无增长、节点空间不足不产生公开目标、共享成员计费、S3 未提交复制故障恢复，以及源复制时实际持有节点和所有者预留。从平台下载派生产物后，真实运行 blockMesh、checkMesh、laplacianFoam 并产生 `1/T`；这证明下载的派生输入可执行，不宣称本测试验证了平台 Job 的整条提交链。

首次 S3 派生测试 `tmp/run-derive-capacity-jp9v6yab/checks.json` 失败：观察器把普通文件写入的 before-image 备份误当成新增远端复制，错误断言节点预留。已限定观察范围为实际 `RemoteSource` 分支，失败记录未删除。正常故障测试日志中的 `intentional failure after real ...` 为预期注入，须与未预期异常区分。

4 个临时 PostgreSQL 数据库删除回执位于 `tmp/mixedread-postgres-{9khlh_bf,a65490c0,36h_upxf,8qufedj8}/cleanup.json`。开发数据库及端口 8000 未修改。

## 工况派生首次源扫描与同案保存

2026-11-19。`POST /api/v1/cases/{case_id}/runs:derive` 先在短事务中核验写权限并记录工作区、实际存储前缀、字典版本和算例持久事件序号。完成事务并释放所有者额度后，执行原有的首次源目录枚举及 SHA-256 扫描。随后在算例写协调中重新核验权限、源绑定和目标是否存在。算例在扫描期间发生持久事件时，这次请求会保守地返回 409，用户可以按同名重试。

目标的实际复制、节点额度、逐文件摘要、提交前全目录复核、原子发布或文件事务恢复继续走原路径。第一次扫描仍读原先需要的源字节，两个短事务之间没有新增一次文件体重读。此次实测覆盖首次扫描、并发保存和过期拒绝，最终复制及复核仍在原写入范围内。

实际测试在第一个真实数据块后暂停首扫，并从独立 SQLite 连接探测写入事务。并行请求通过普通 `PUT /file` 按旧 SHA 保存同一基准工况中的另一文件。每组首扫中受观测的大文件实际读取 41,943,040 字节，共 40 个 1 MiB 数据块。真实和模拟模型调用均为 0。

| 环境 | 首扫时独立 SQLite 写事务 | 同案保存 | 派生结果 |
| --- | --- | --- | --- |
| 原版 SQLite / Windows FS | 无法开始 | 409 `storage_quota_busy` | 200，得到保存前的基准版本。释放后补存 200。 |
| 固定版 SQLite / Windows FS | 可开始 | 200 | 旧派生 409，按同名重试 200。 |
| 固定版 SQLite / 独立认证 SeaweedFS | 可开始且首扫无所有者写额度 | 200 | 旧派生 409，按同名重试 200。 |

两个固定组在旧请求 409 后均观察到目标写入调用 0 次、原始文件入口 404、实际目标目录或 S3 前缀为空、目标 `files.changed` 事件 0 条。重试恰好提交 3 个文件，包括新保存内容与完整初场大文件。目标 40 MiB 的流式回读与本组原件 SHA-256 完全一致，结果时刻、处理器目录、日志和 `.foam` 仍被过滤。同名再提交返回 409，另一用户访问返回 404，节点实际预留回到 0 字节。 [原版 FS](../../tmp/run-derive-source-fs-_e3dh_r_/run-derive-source-lock.json) · [固定 FS](../../tmp/run-derive-source-fs-gg24h8mm/run-derive-source-lock.json) · [固定 SeaweedFS](../../tmp/run-derive-source-s3-1tdl6win/run-derive-source-lock.json)

精确审查 66 个源的正式执行器原版对照 `1/1`（9.75 秒），固定双后端 `1/1`（113.25 秒）。应用启动器的退出码为 1，第一次监听探测尚未确认端口关闭。随后独立核对 3 个 Windows API 进程及其监听均消失，SeaweedFS 启动器退出码为 0、实际 WSL 进程和 8 个监听均已消失。临时目录和证据位于 D 盘。固定验收前后 C 盘可用空间为 21.02 → 21.09 GiB，Docker 构建缓存为 0 B。此次验收覆盖 N4-05 原合同中的该入口。 [原版执行器](../../tmp/vibeflow-regression-all-20261119-194026-8190b7/summary.json) · [固定执行器](../../tmp/vibeflow-regression-all-20261119-194529-398919/summary.json) · [独立关停复核](../../tmp/run-derive-source-staging-20261119/os-exit-proof.json)

## 剩余边界

文件系统的私有目录恢复历史计入实际磁盘用量。认证 S3 继续沿用每次 256 个文件、单文件 1 GiB 和含备份合计 2 GiB 的事务限制。超过支持范围时会明确拒绝。两种后端均完成同名竞争和提交前退出恢复。物理多主机部署由 N7-01 跟踪。

RB-12 还包括 ZIP/备份/导出峰值及剩余长文件入口；几何 native 临时目录、转换内存/期限、长扫描和全局并发由 RB-13 至 RB-17 继续。旧共享上传记录归属、未知可变元数据请求、跨 worker 上传取消、上传请求体流式限额与永久关闭标记维护仍保留。账户网站认证联调按用户决定暂缓，不影响其余已授权工作。
