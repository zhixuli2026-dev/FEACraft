# 文件目录扫描的数量和期限

## 2026-11-20 公开文件树锁外扫描

`GET /tree` 与 `POST /tree:patch` 先在短事务读取事件水位和存储标记，释放算例锁及连接后扫描文件和目录。返回前重新读取用户权限、算例来源、未结存储操作、事件水位和存储标记。变化时在原请求期限内重新扫描一次。再次变动返回 `409 file_tree_changed`。两次扫描之间释放上一份结果。[路由](../server/app/routers/files.py) · [存储标记](../server/app/services/case_tree.py)

本地使用算例根和文件、目录事务目录的身份及时间。待决目录安装和归档属于同一协议，已回滚的文件变更也会改变标记。S3 使用物理目录清单的 ETag，最终核对只请求元数据。S3 初始读取仍执行原目录清单隔离与旧版兼容逻辑。继续沿用公开响应格式和路径提示回退约定。

实际文件系统的整树扫描读取 1,024 个种子文件。SQLite 在扫描暂停时可取得独立写事务，PostgreSQL 可立即取得同一算例的行锁。认证 SeaweedFS 的对象枚举暂停时也观察到两种数据库的对应结果。两种存储上的同案改动会触发重读，返回包含新路径及新水位的整树，路径刷新读到更新后的大小。账户停用期间的在途读取返回 403。文件系统另有真实文件事务在 SQL 提交前写入临时版本、整树已读到该路径、随后回滚并归档的场景。最终结果排除了该路径，水位保持原值。[SQLite 文件系统](../../tmp/file-tree-lock-pko71_1z/file-tree-lock.json) · [PostgreSQL 文件系统](../../tmp/file-tree-lock-qq6l5z9h/file-tree-lock.json) · [SQLite 与认证 SeaweedFS](../../tmp/file-tree-lock-tq5x0qk0/file-tree-lock.json) · [PostgreSQL 与认证 SeaweedFS](../../tmp/file-tree-lock-4wy1jwl7/file-tree-lock.json)

原本地文件事务诊断、复制超时、竞争修改、第二次 API 退出及自动接续通过复验。两轮临时 PostgreSQL 库已删除。[恢复兼容](../../tmp/tree-scan-file-recovery-compat-20261120.json) · [文件系统临时库](../../tmp/terminal-agent-postgres-6ae6c0y7/cleanup.json) · [认证 S3 临时库](../../tmp/terminal-agent-postgres-00y761v9/cleanup.json)

本节实际基础设施及故障测试真实模型 0 次，模拟 0 次。专项从服务器虚拟环境执行 `file_tree_lock_acceptance.py`，可选 `--postgres` 和 `--s3`。无数据库迁移，回退可复用上一版读取实现和现有存储格式。[专项入口](../server/file_tree_lock_acceptance.py)

## 2026-11-20 文件树写锁基线

用隔离的 Windows 文件系统和 SQLite 通过公开 `GET /cases/{id}/tree` 扫描 1,024 个种子文件。将真实文件枚举暂停在读到第一条时，扫描仍在算例事务中，独立 SQLite `BEGIN IMMEDIATE` 返回数据库已锁。松开扫描后，请求返回完整 1,062 条行和原事件水位。该结果定位了 RB-14 中公开整树读取的锁范围。真实模型 0 次，模拟 0 次。[实测回执](../../tmp/file-tree-lock-h9fxtyah/file-tree-lock.json) · [验收入口](../server/file_tree_lock_acceptance.py)

2026-11-15，N4-05 原 RB-14/RB-15 增量。主线 48/53、剩 5 项；资源 11/17、剩 6 块。

原 `storage.list_tree` 在上层检查文件数之前就收集完整目录。FS 的 `Path.rglob` 以及 S3 逻辑分页器内部的完整物理/逻辑合并都可能无界增长；仅限制返回页大小不能限制这个过程。

现在复用 `storage_measurement_max_entries` 和 `storage_measurement_timeout_seconds`，默认分别为 1,000,000 个扫描条目和 30 秒。每次扫描在接收下一项、下一页前后检查预算，并继承更早的请求截止时间。FS 使用实际 `scandir` 逐项枚举，空目录也计数，句柄在错误时关闭；S3 的原始分页及逻辑目录合并均有检查，隐藏物理项和逻辑 tombstone 也消耗扫描预算。已存在的逻辑 HEAD 大小限制保持。

超量抛出 `storage_listing_limit`，本次扫描期限到期为 `storage_listing_timeout`，沿用结构化冲突处理；继承的请求期限继续抛出原 TimeoutError。只返回完整、排序后的结果，不返回截断目录。公共路径仍相对算例，内部 S3 清单仍相对所查前缀。扫描没有删除、迁移或覆盖对象。

真实验收入口 `server/storage_listing_acceptance.py` 经 LLM 审查登记后执行。结果 `../../tmp/storage-listing-wpvi4crv/acceptance.json` 的 9 组均通过：FS 恰好达到限额成功、减一拒绝、空目录计数、实际枚举暂停后的两种期限及句柄关闭；真实 SeaweedFS 的 1,025 个对象两页完整读取、超额拒绝、第一张实际 HTTP 页暂停后不再请求第二页；实际逻辑 CAS/删除/私有文件/子目录字节兼容、逻辑目录条目计数、拒绝后的再次完整读取。

兼容验收只选择已审查的 `archive_capacity_checks.py`：FS/SQLite 18 项 `../../tmp/archive-capacity-v7sv_6qy/checks.json`、S3/SQLite 20 项 `../../tmp/archive-capacity-9jt56gla/checks.json` 全部通过。涵盖 ZIP 导入、实际备份/恢复、二进制及元数据、额度拒绝、真实线程取消和清理。两套 API 已退出；扫描测试和兼容测试各自的 SeaweedFS 服务都已由夹具关闭。本轮没有运行 PostgreSQL 组合。

本轮模型调用 0、模拟 LLM 0。限额降低和真实读取之后的暂停是明确故障条件，数据和网络结果始终来自实际存储。已经发出的单次同步 OS/SDK 调用仍需返回后才能检查到期；这不是瞬时中断或 RSS 硬额度。原长期 I/O 队列、跨节点额度和原生进程外部资源边界继续按原清单完成。
