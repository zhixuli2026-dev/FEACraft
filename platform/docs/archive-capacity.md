# ZIP 导入与工作区备份的额度和取消收尾

## 2026-11-21 工作区备份恢复的正文安装

原公开恢复入口在创建多算例的 SQL 写事务内复制正文。对实际导出的 v1 ZIP 暂停 12 MiB 最终复制时，独立 SQLite 写入失败。放行后读取的 SHA-256 与源文件一致。[旧版实测](../../tmp/workspace-restore-install-baseline-6gum9we7/baseline.json)

现在先把所有算例的正文准备在独立目录或对象前缀，校验并固定版本后关闭解码暂存。最终事务分配工作区、算例和作业 ID，并把日志映射到 `.vibeflow/jobs/job-{id}.log`。同卷文件系统改名后原子安装目录，认证 SeaweedFS 用条件更新目录清单，保留同一对象键和 ETag。一个工作区的算例、作业、会话和存储见证仍在同一次 SQL 提交中生效。S3 已绑定但尚未改名的待决清单和改名后的清单都可按原证据回滚。[恢复实现](../server/app/services/backup.py) · [正文暂存](../server/app/services/backup_restore.py) · [对象清单更新](../server/app/services/s3_live_store.py)

| 文件存储 | SQLite | PostgreSQL |
| --- | --- | --- |
| Windows 文件系统 | [回执](../../tmp/workspace-restore-install-rly0vs69/restore-install.json) | [回执](../../tmp/workspace-restore-install-puerp2sv/restore-install.json) |
| 认证 SeaweedFS | [回执](../../tmp/workspace-restore-install-xib5_ejq/restore-install.json) | [回执](../../tmp/workspace-restore-install-z6d2y88h/restore-install.json) |

四组均通过两算例、两个同名旧日志、12 MiB 原件、空文件和会话分支读回。文件系统在正文期间验证同账户另案保存，认证 S3 沿用账户写入凭证并验证其他账户保存。五次实际 API 退出覆盖本地原子安装后及 S3 清单更新前后，原包重试后节点预留均归零。SQLite 文件系统验证等长且保留修改时间的改写返回 409，超时排空后返回 504。SQLite 文件系统与 PostgreSQL 认证 S3 均通过复导出的 v1 ZIP 二次恢复。另有 40 MiB StandardCase 导入兼容复验。真实模型与模拟均为 0。[导入回归](../../tmp/standard-case-stream-import-_yujzjeb/standard-case-stream-import.json)

本地跨卷恢复仍走兼容复制路径。后续已将冷启动回滚正文核验移出写事务，四组合证据另行记录。RB-12、RB-14 继续开放。[冷回滚复验](constructor-recovery.md#2026-11-21-冷启动构造回滚)

## 2026-11-21 StandardCase 大文件安装

旧版公开导入将解压后的 40 MiB 正文再复制到目标目录。暂停实际复制后，独立 SQLite 连接获取写事务失败，放行后导入和原件读回均成功。[旧版实测](../../tmp/import-install-baseline-9cizurqf/baseline.json)

同卷文件系统沿用算例构造见证，校验解压目录和记录文件版本后，事务内重核版本并原子安装已有目录，省去另一份正文副本。认证 SeaweedFS 在写事务外上传、校验和记录对象版本，最后用条件写入绑定原待决清单与算例。S3 的账户容量凭证继续保护传输，由其他账户验证并发保存。Windows 同卷文件系统验证同账户另案保存。[本地导入实现](../server/app/services/case_import.py) · [远端待决清单](../server/app/services/case_provision.py)

| 文件存储 | SQLite | PostgreSQL |
| --- | --- | --- |
| Windows 文件系统 | [回执](../../tmp/import-final-install-hhi38pcj/final-import.json) | [回执](../../tmp/import-final-install-qywl4hjn/final-import.json) |
| 认证 SeaweedFS | [回执](../../tmp/import-final-install-ei90lv5r/final-import.json) | [回执](../../tmp/import-final-install-0krh7412/final-import.json) |

四组实测均在正文阶段暂停时保存另一算例，40 MiB 原件下载摘要一致。四次 API 实际退出覆盖文件系统安装后和 S3 正文上传后，冷启动回滚原待决清单，同名重试成功，私有目录与节点预留清空。Windows 等长并保留修改时间的改写返回 409，SQLite 文件系统超时在 I/O 排空后返回 504。原 40 MiB 导入回归继续通过，保留自定义几何脚本和空文件。真实模型和模拟均为 0。[大包兼容回执](../../tmp/standard-case-stream-import-dbyy5la6/standard-case-stream-import.json)

不同卷的本地算例目录仍走原复制路径。工作区 ZIP 恢复也已用 12 MiB 真实文件复现最终复制阻塞 SQLite 写入，下一步处理其多算例和日志重映射。RB-12、RB-14 继续开放。[工作区恢复基线](../../tmp/workspace-restore-install-baseline-6gum9we7/baseline.json)

2026-11-06。N4-05 / RB-12 增量，并补充 FF-A25、FF-J12 的实际证据。主线仍为 33/53 完成、剩 20 项；资源清单 11/17 完成、剩 6 块。未将检查条数当作已完成任务数。

## 修改

StandardCase ZIP 导入的暂存目录改到实际存储根目录，纳入节点物理用量。读取框架已接收的上传文件后，按实际压缩包长度预留节点空间再复制；解压前验证中央目录的文件数、单文件/累计大小及压缩比，再预留全部解压内容大小。复制和解压在工作线程执行，取消等待线程结束；暂存清理也排空后才退出所有者额度范围。SQL 提交/回滚等待完成。

解压逐块检查实际长度后写出，拒绝超出核定大小的字节。上传包与展开文件分开存放，因此包内合法的 `upload.zip` 文件不会再碰撞暂存包。增加可移植路径及文件/父目录冲突检查，保留路径越界、符号链接、加密、大小写冲突和压缩炸弹限制。

工作区备份在工作区/算例锁之前取得 S3 所有者额度，覆盖协调器隐含恢复和事务结算。它先核对整个工作区的源文件数量/总大小，再读取和哈希。S3 读取受对象版本条件与核定长度约束；FS 读取检查文件身份和长度。备份后复核源清单。

文件读取、校验、ZIP 压缩和恢复包解析移到排空式工作线程；异步 SQL 不进入线程。manifest 逐段编码并检查 160 MiB 上限，ZIP 输出在写入前检查 100 MiB 上限，不再同时持有整份 JSON 编码副本。备份仍采用原 v1 `manifest.json` 格式，恢复仍使用已有的算例创建见证协议。

## 验收

显式入口 `platform/server/archive_capacity_checks.py`。审查过其导入的隔离 API、真实 S3、临时 PostgreSQL 及迁移辅助函数；未运行旧回归集合。本次无 LLM 调用、无模拟模型回复。取消测试只在真实 ZIP 读取/压缩完成边界使用线程屏障，返回的文件内容始终来自实际文件操作。

| 存储 / 数据库 | 证据 | 检查数 |
| --- | --- | --- |
| FS / SQLite | `tmp/archive-capacity-grkujb9s/checks.json` | 18 |
| FS / PostgreSQL | `tmp/archive-capacity-k3s_u85u/checks.json` | 18 |
| S3 / SQLite | `tmp/archive-capacity-cg3f27yv/checks.json` | 20 |
| S3 / PostgreSQL | `tmp/archive-capacity-cx2n7jnr/checks.json` | 20 |

共 76 条实际检查通过，四组 API 日志均无未预期异常栈。覆盖：

- 压缩包复制、解压分别空间不足，拒绝后没有公开算例或遗留暂存。
- 实际多层根目录、64 KiB 随机二进制和名为 `upload.zip` 的文件导入；同名算例拒绝。
- 越界路径、文件/父目录冲突、保留设备名、压缩炸弹、实际 CRC 损坏拒绝与清理。
- 解压线程仍工作时取消，等待线程排空再清理；不创建半成品算例。
- 两个真实算例备份，二进制字节准确，排除文件事务内部日志；实际恢复并核对文件及算例元数据。
- 源文件、manifest、压缩输出三种额度分别拒绝。测试降低对应阈值以触发同一生产检查路径，并未伪造文件大小或压缩结果。
- 压缩线程仍工作时取消，等待其结束并结算；损坏恢复包释放额度。
- S3 零账户额度下，备份、导入、恢复均拒绝且物理用量不增长。
- 实际节点预留、所有者额度在相应 I/O 边界存在；结束后节点预留和导入暂存均清空。

临时 PostgreSQL 删除回执：`tmp/mixedread-postgres-z477xbsr/cleanup.json`、`tmp/mixedread-postgres-_qh36839/cleanup.json`。全部测试进程已退出；开发数据库及端口 8000 未修改。

## 剩余范围

这不等于完整内存/全局并发限制：v1 备份仍在内存保存文件的 Base64 清单及最终 ZIP，SQL 元数据和全部请求的并发内存预算属于 RB-14/RB-16。目录枚举也仍需有界扫描。框架接收 multipart 时的前置 spool、系统目录与其他卷资源边界属于 RB-17。

本轮证明请求异常/取消的暂存清理，没有证明进程被强制终止后的自动清理；进程退出留下的 `.case-import-*` 文件纳入节点物理用量，不按 TTL 猜测删除，维护协议由 RB-15 继续。

仍需检查报告/几何等任务提交前恢复及原生调用交接、技能包和远程目录导出、上传交错恢复。静态候选清单 `tmp/archive-capacity-followup-candidates.json` 只用于定位调用链，不能把未出现局部额度关键词当作缺陷证据；部分入口通过外层辅助函数已取得额度。

FF-A25 与 FF-J12 保持未完整完成：大包/多 worker/猝死与完整权限矩阵、真正的升级回滚演练仍须另行证明。53 项主线和 295 项功能范围不变。

2026-11-12（备份/恢复跨worker准入）：同节点根共享非阻塞OS名额，覆盖正文解析、真实线程排空及64KiB分块响应；默认整次300秒，忙时429，到期排空后504，API实际退出自动释放。不同用户/双API6组实际边界及原FS归档18项兼容通过，初始5组不重复计数；5个实际API结束，模型0、模拟0。主线48/53、剩5；资源11/17、剩6，295原范围保持。此为单节点大包并发约束，未声称全入口/多节点或RSS硬限额。见[实现、证据与下一步](archive-cross-worker-admission.md)。
