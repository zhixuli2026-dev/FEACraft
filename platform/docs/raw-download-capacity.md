# 普通文件下载的期限、内存与暂存额度

原 `/raw` 入口在算例锁内同步读取并组装完整正文。现在先在原协调范围选定文件版本，再释放业务数据库事务和 S3 恢复额度。完整 GET、单段 Range 和多段 Range 使用有界线程按 64 KiB 读取。正文准备、头部校验及版本检查完成后才发送响应。打开前或者分段间的版本变化仍返回 `409 file_changed_during_read`。[文件路由](../server/app/routers/files.py) · [文件版本校验](../server/app/services/file_read.py)

每个 API 进程最多接纳四个同时准备或发送的 GET。每个响应在内存暂存最多 1 MiB，超过时先在节点额度中按完整响应长度申请一次预留，再将内容写入临时文件。容量不足在读取正文前返回 `409 node_storage_full`，名额用尽返回 429。准备共用一个配置期限，正在运行的读取先排空再报告 504。传输总期限为 300 秒，正常发送和连接断开均在 ASGI 请求退出时关闭临时文件并释放预留。HEAD 只选定版本和构建响应头。[请求生命周期](../server/app/raw_download.py) · [暂存实现](../server/app/services/backup_download.py)

## 四组实际验收

| 业务数据库和存储 | 资源与响应记录 | 真实 DeepSeek 回执 |
|---|---|---|
| SQLite 与文件系统 | [结果](../../tmp/live-cli-startup-m6wwpbv1/raw-download-capacity.json) | [回执](../../tmp/live-cli-startup-m6wwpbv1/live-result.json) |
| PostgreSQL 与文件系统 | [结果](../../tmp/live-cli-startup-lysyfnti/raw-download-capacity.json) | [回执](../../tmp/live-cli-startup-lysyfnti/live-result.json) |
| SQLite 与 SeaweedFS | [结果](../../tmp/live-cli-startup-dac4utuz/raw-download-capacity.json) | [回执](../../tmp/live-cli-startup-dac4utuz/live-result.json) |
| PostgreSQL 与 SeaweedFS | [结果](../../tmp/live-cli-startup-7ujfoofx/raw-download-capacity.json) | [回执](../../tmp/live-cli-startup-7ujfoofx/live-result.json) |

四组各完整下载并核对 8,388,649 字节，源读取 129 块，单块最多 65,536 字节。暂停正文读取时，同一算例可提交真实文件替换，原下载随后返回 409。保持四个实际 HTTP 连接等待发送时，四份预留各为 8,388,649 字节，第五个请求被拒绝，另一个文件仍可保存。主动断开后预留全部释放。缩短准备期限的故障检查在超时后仍观测到阻塞线程持有额度，解除阻塞后返回 504，再核对预留归零。磁盘额度不足、异账户、HEAD、合法多段及重叠区间均有实际断言。[专项入口](../server/live_llm_raw_download_capacity_acceptance.py)

既有一致性套件按审查登记复跑。文件系统 17 项及 S3 16 项实际 HTTP 检查通过，覆盖打开前替换、段间替换或删除、读完整版本后替换及跨 API 版本读取。另各有 6 项对象存储协议故障检查，包含忽略条件、错误范围和短正文。两种生产前端浏览器下载兼容均通过，包含私有 S3 签名和启用桶版本控制的历史兼容。最终四组专项加两组浏览器兼容共 12 次真实 `deepseek-flash` HTTP 200，模拟模型调用为 0。[文件系统一致性](../../tmp/file-read-consistency-1tgazg7a/download-result.json) · [S3 一致性](../../tmp/file-read-consistency-3dot1k77/download-result.json) · [文件系统浏览器](../../tmp/live-cli-startup-rqo2nm12/file-download.json) · [S3 浏览器](../../tmp/live-cli-startup-9xenw1g4/file-download.json)

首轮文件系统验收已记录分块读取，其发送故障注入模块缺少 `asyncio` 导入。修正测试入口后，四种组合完整复验通过。正式复现使用服务端虚拟环境运行专项入口，传入 `--source-profile-id 26`，按需添加 `--postgres`、`--s3`。随应用版本回退，无数据库迁移。发布成果等独立原件入口继续按各自清单校验和资源条目跟踪。[首轮验收记录](../../tmp/live-cli-startup-vlnaxzwv/live-result.json) · [资源清单](resource-budget-worklist.md)

## 2026-11-21 报告草稿原件

旧 `/report/compose-artifact` 在算例协调期间读取全文。实际 OpenFOAM 和 VTK 生成报告后，测试在原正文读操作前暂停，健康检查的一秒请求出现超时。释放后返回原草稿字节。[旧入口实测](../../tmp/live-cli-startup-zjd_02tg/compose-download-capacity.json)

接口现与报告派生原件共用有界下载。短事务确定算例、成功作业和不可变发布身份，结束事务和 S3 恢复额度后再读取并核验归档。GET 支持原整文下载、Range 与版本校验，HEAD 提供长度和版本。保留显式发布版本、当前草稿选择及原附件文件名。正文使用既有 1 MiB 内存暂存阈值和节点磁盘额度，超过阈值时在读取前预留响应空间，请求结束时关闭暂存并释放名额。[报告路由](../server/app/routers/reports.py) · [归档读取](../server/app/services/report_history.py) · [下载生命周期](../server/app/raw_download.py)

SQLite 文件系统及 PostgreSQL 认证 SeaweedFS 实测暂停档案正文时健康检查通过，同账户的另一个算例可以写入。原草稿及派生入口返回相同字节，单段 206、HEAD、不可满足区间 416、错误版本 409 和跨算例 404 均符合预期。实际 Windows 作业写入的 8,840,000 字节原稿经报告重新组稿保留到历史发布，流式下载后独立核对全文 SHA-256。[文件系统验收](../../tmp/live-cli-startup-6df9r1bf/compose-download-capacity.json) · [认证 S3 验收](../../tmp/live-cli-startup-19avhjga/compose-download-capacity.json) · [专项入口](../server/live_llm_compose_download_capacity.py)

首轮改动与同路由模块的另一个处理函数重名，文件系统和 S3 请求都返回 409。改名后按原场景复验。[文件系统首轮](../../tmp/live-cli-startup-knpbi7r0/live-result.json) · [S3 首轮](../../tmp/live-cli-startup-rhfdwztn/live-result.json)

基线及两组最终复验共 6 次真实 `deepseek-flash` 文件回读，均为 HTTP 200，模拟 0 次。专项运行用服务端虚拟环境执行上述入口，传入 `--source-profile-id 26`。旧版对照追加 `--baseline`，PostgreSQL 与认证 SeaweedFS 追加 `--postgres --s3`。[基线回执](../../tmp/live-cli-startup-zjd_02tg/live-result.json) · [文件系统回执](../../tmp/live-cli-startup-6df9r1bf/live-result.json) · [认证 S3 回执](../../tmp/live-cli-startup-19avhjga/live-result.json)
