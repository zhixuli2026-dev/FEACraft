# FF-H10 索引重建事务与跨进程恢复

## 2026-11-19 索引状态读取的事务与期限

文件树调用的 `GET /api/v1/cases/{case_id}/index/status` 保留未建索引和已发布状态的响应格式。入口先以短事务完成算例授权和存储恢复，再使用共享有界线程池持有同一请求期限核验正文。结果返回前重查账户、算例权限和目录绑定。[生产入口](../server/app/routers/index.py) · [验收入口](../server/index_status_io_smoke.py)

旧版在 SQLite 和文件系统上读到实际 4,194,304 字节文件时持有业务写事务，独立写连接无法获取锁，正式条件保存返回 409。修复版在实际文件系统与认证 SeaweedFS 上均返回保存 200，过期的状态查询返回 `index_source_changed` 409。旧索引保持，重试后发布新索引。实际读取排空后返回 504，期限后没有开始新读取，重试保留相同构建标识。查看者获准后能读取。读取中撤权后，正在等待的请求和下一请求均返回 404。[旧版](../../tmp/index-status-io-fs-1s810t6x/index-status-io.json) · [文件系统](../../tmp/index-status-io-fs-uzktxyun/index-status-io.json) · [SeaweedFS](../../tmp/index-status-io-s3-je30jfui/index-status-io.json)

三个隔离 API 通过原进程身份核验，认证 SeaweedFS 包装器退出 0，八个存储监听端口均已关闭。首轮旧版对照在包装器退出后立即探测，观察到 API 子进程仍在退出。夹具后续轮询同一个进程身份直到实际退出。两组固定验收、本组基线及独立端口核查均通过，真实模型调用和模拟调用均为 0。[独立端口核查](../../tmp/index-status-io-s3-je30jfui/listener-exit-proof.json)

## 2026-11-19 工作区搜索的事务与读取期限

工作区 `GET /api/v1/workspaces/{ws_id}/search` 对每个算例先完成授权后的恢复阶段，再在业务事务之外读取文件和对账索引。现有有界线程池对索引扫描及聚合共用一个请求期限。读完后重查账户、工作区权限和已经选择的算例，按原顺序汇总。[生产入口](../server/app/routers/workspaces.py) · [验收入口](../server/workspace_search_io_smoke.py)

旧版 SQLite 与文件系统在实际读取 4,194,304 字节文件时暂停，独立业务写连接不能获取锁，同算例条件保存返回 409。固定版在同节点的实际文件系统与认证 SeaweedFS 上均允许并发保存，返回 200。失效的原搜索随后返回 409，旧索引保持，重试可读到新版本。期限耗尽后排空原读取，返回 504，随后的读取计数为 0。三个算例中两个已索引，命中顺序仍为 1、2。查看者依次被拒绝、授予权限、撤回权限，返回 404、200、404。[旧版实测](../../tmp/workspace-search-io-fs-u1b_ikk4/workspace-search-io.json) · [固定版文件系统](../../tmp/workspace-search-io-fs-0z6wh8s4/workspace-search-io.json) · [固定版 SeaweedFS](../../tmp/workspace-search-io-s3-miirekoz/workspace-search-io.json)

正式固定回归为 1/1，95.609 秒，核验三个隔离 API 原进程退出、实际 SeaweedFS 包装器退出及八个 Linux 存储端口关闭。模型和模拟调用均为 0。此验收补齐 RB-14 的工作区搜索入口。[正式回归](../../tmp/vibeflow-regression-all-20261119-213345-0d6cc8/summary.json) · [独立关停核验](../../tmp/workspace-search-io-stage-20261119/os-exit-proof.json)

旧版在独立进程首建索引时，FTS 已写入索引事务但尚未提交，索引状态接口返回 HTTP 500。[旧版实测](../../tmp/live-cli-startup-3d0f_v00/index-rebuild.json)

重建现在以 SQLite 写事务为跨进程排他依据。已提交的构建标识、文件行和 FTS 行在同一事务内发布。状态与搜索可在写入期间读取上一份 WAL 快照，首建提交前返回未就绪。新进程退出后由 SQLite 回滚未提交结果并释放锁，下一次重建可以立即进行。旧版本留下的近期 `.building` 文件仍会阻止重建，确认取得写锁且标记超过原 600 秒后将其归档。

重建入口先按原流程授权并恢复文件事务，再释放算例和账户锁。索引扫描在现有有界文件线程池执行，复用存储操作期限，新内容在提交前重新核对目录版本。活跃写事务返回 `409 index_build_busy`。构建期间源文件变化返回 `409 index_source_changed`，文件写入保留，前一份索引可以读取。

## 实际验收

| 业务数据库和文件存储 | 重建记录 | 真实模型回执 |
|---|---|---|
| SQLite 与文件系统 | [结果](../../tmp/live-cli-startup-9bdr135a/index-rebuild.json) | [DeepSeek](../../tmp/live-cli-startup-9bdr135a/live-result.json) |
| PostgreSQL 与文件系统 | [结果](../../tmp/live-cli-startup-zj46ni5r/index-rebuild.json) | [DeepSeek](../../tmp/live-cli-startup-zj46ni5r/live-result.json) |
| SQLite 与 SeaweedFS | [结果](../../tmp/live-cli-startup-nwqq2bil/index-rebuild.json) | [DeepSeek](../../tmp/live-cli-startup-nwqq2bil/live-result.json) |
| PostgreSQL 与 SeaweedFS | [结果](../../tmp/live-cli-startup-ouowdc2c/index-rebuild.json) | [DeepSeek](../../tmp/live-cli-startup-ouowdc2c/live-result.json) |

四组各终止两次实际重建进程，分别在首次构建和替换 FTS 写入后、提交前终止。首建无可见半成品。替换失败后从只读 SQLite 直接核对原构建号与内容摘要。活动索引与冲突期间的原快照可读，冲突请求耗时约 0.31 至 0.59 秒。每组还在一个真实 API 请求暂停重建时执行第二个重建、查询旧索引并成功写入算例，验证源变化让第一请求回滚，显式重试后新内容可查。四组验证旧锁归档及异账户三条接口均返回 404。

两组 S3 首跑时，独立重建进程未继承隔离对象存储配置，误用文件系统并在扫描目录时失败。显式传入同一隔离对象存储配置后重跑通过。对应运行未发起模型请求。[SQLite 首跑](../../tmp/live-cli-startup-l6wxtwd7/live-result.json) · [PostgreSQL 首跑](../../tmp/live-cli-startup-ejex6o1l/live-result.json)

独立的真实工具回读每组使用 2 次 `deepseek-flash`，共 8 次 HTTP 200，模拟调用为 0。验收入口在服务端虚拟环境中使用 `--source-profile-id 26`，按部署组合添加 `--postgres` 和 `--s3`。同主机共享本地 SQLite 索引的部署约束仍适用。索引为衍生数据，随应用版本回退即可，无需数据库迁移。[验收入口](../server/live_llm_index_rebuild_acceptance.py) · [共享索引运维](shared-index-deployment.md)

另用原生产前端构建连接当前隔离 API，在 PostgreSQL 的两种存储组合实际点击重建，完成单算例搜索、工作区搜索及打开命中文件。只读共享成员能查看状态并搜索，重建和撤销后的读取均返回 404。两轮另有 4 次真实 DeepSeek HTTP 200。补跑首轮缺少 Playwright 解释器环境变量，补齐后通过。[文件系统浏览器](../../tmp/live-cli-startup-kbeeob93/workspace-search.json) · [S3 浏览器](../../tmp/live-cli-startup-arr_940r/workspace-search.json) · [首轮环境错误](../../tmp/live-cli-startup-shl5q5t8/live-result.json)

即时索引兼容在文件系统与 SQLite、S3 与 PostgreSQL 各选一轮最终通过的结果，覆盖普通上传、分块发布、caseDict、两种 Agent 写入与持久事件补齐，共 12 次真实 DeepSeek HTTP 200。S3 第一次兼容复验记录了 4 次实际请求，自动写入后未命中预期正文。留存的 SQLite 显示该路径已及时索引，索引正文与登记的摘要一致，但长度只有 33 字节，预期标记为 37 字节。独立复跑通过，随后增加工具参数、实际文件及索引摘要的诊断，并再次通过。[文件系统兼容](../../tmp/live-cli-startup-n8cddn84/immediate-file-index.json) · [最终 S3 兼容](../../tmp/live-cli-startup-621uz69e/immediate-file-index.json) · [S3 首次失败诊断](../../tmp/live-cli-startup-86e73xuy/direct-write-index-diagnosis.json)

## 单算例搜索的写锁与读取期限

2026-11-19。公开 `GET /api/v1/cases/{id}/search` 先在短事务中完成权限检查和已有文件事务恢复，记录工作区和存储前缀。结束业务事务后，在现有有界文件线程池完成索引对账和全文查询。授权、读取及返回前的权限和前缀复核共用已有存储操作期限。索引仍使用自身的 SQLite 写事务和 WAL 快照，原刷新间隔、FS 文件标识与时间戳、S3 对象版本、正文摘要及未变 FTS 行沿用原规则。读取发现已知缺文件或原尺寸校验冲突时返回 `409 index_source_changed`，实际存储或服务错误继续传播。共享枚举与对账在当前期限生效时也会检查截止点。

隔离验收暂停后台索引投递，从正常文件接口写入 4,194,304 字节正文，通过正式搜索请求读完该文件后暂停线程。旧版线程没有继承请求期限，独立 SQLite 连接无法开始写事务，同一算例的普通 CAS 保存得到 `409 storage_quota_busy`。释放线程后搜索返回 200 并读到原索引。首次准备时算例名含 `/`，创建返回 400，修正名称后才进入旧版实测。 [首次夹具失败](../../tmp/vibeflow-regression-all-20261119-200205-c46c2c/index_search_io_smoke.log) · [旧版实测](../../tmp/index-search-io-fs-8iox8f9w/index-search-io.json)

固定版在 SQLite 与 Windows 文件系统、SQLite 与独立认证 SeaweedFS 各通过一次完整验收。

- 线程暂停时独立 SQLite 写事务可开始，同案等长 CAS 保存返回 200。搜索识别源变化并返回 409，旧索引的构建标识、内容、FTS 行号与验证时间全部保持。下一次公开查询主动索引新值，原 FTS 行号仍为 14。
- 夹具把既有存储操作期限配置为 8 秒。到期时请求等待实际读取线程排空。释放后返回 `504 case_search_timeout`，观察到到期后的正文与目录读取均为 0 次，索引验证时间保持原值。正常重试确认相同结果并更新验证时间。
- 同案 CAS 删除返回 200，原始文件随后返回 404。在旧清单上的读取真实遇到 FS `FileNotFoundError` 或 S3 逻辑对象 404，搜索返回 409 并保留原投影。正常重试从 FTS 移除已删除的内容。 [固定 Windows FS](../../tmp/index-search-io-fs-ptl8ykns/index-search-io.json) · [固定 SeaweedFS](../../tmp/index-search-io-s3-wntbfejs/index-search-io.json)

执行器通过 68 个精确源码审查。旧版对照 `1/1`（6.421 秒），固定双后端 `1/1`（83.703 秒），真实与模拟模型调用均为 0。独立确认四个测试 API 进程和端口结束，以及 SeaweedFS 启动器退出码为 0、WSL 实际进程和八个端口结束。夹具和证据位于 D 盘，固定版前后 C 盘可用空间为 22.03 → 22.02 GiB，Docker 构建缓存为 0 B。此次公开入口范围为单算例搜索，RB-14 仍按原长 I/O、锁、心跳及其他请求入口合同推进。 [固定执行器](../../tmp/vibeflow-regression-all-20261119-200625-18715d/summary.json) · [独立关停回执](../../tmp/index-search-io-stage-20261119/os-exit-proof.json)
