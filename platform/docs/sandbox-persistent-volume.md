# 沙箱持久卷与实际 API 猝死数据保留

2026-11-07 后续：独立持久恢复操作、状态查询及 ZIP 下载 API 已完成隔离验收，见 [沙箱只读恢复快照 API](sandbox-recovery-api.md)。恢复界面、确认写回冲突、原会话收尾和保留数据清理继续推进。以下记录保留前两阶段当时的实现范围。

2026-11-07。新建沙箱终端的工作目录从内存 tmpfs 改为独立、固定 128 MiB 的 ext4 镜像。普通关闭仍使用前一阶段的受控导出和文件发布；现在实际 API 猝死后，工作文件也能从镜像只读读回。用户可见的恢复入口尚未完成，不能把本阶段称为完整恢复功能交付。

## 实现

API 在创建终端进程之前预留镜像容量，并在节点存储根目录的 `.terminal-export/<随机标识>` 建立私有目录。固定启动门在实际用户命令运行前创建镜像，核对普通文件、固定长度、独占文件锁、文件系统 UUID，并在私有挂载命名空间内挂载。任何已有镜像都禁止重新格式化。

实际镜像是 128 MiB；格式化和日志占用其中一部分，用户可写容量小于 128 MiB。本阶段实际写满得到 ENOSPC，保留的 payload 为 105,906,176 字节。这里的 128 MiB 是镜像硬上限，不承诺有同等大小的净载荷空间。

正常挂载使用 sync、dirsync、nodev、nosuid；原 user/mount/net/PID namespace 和 nobody 命令继续保留。工作目录绑定到该私有文件系统，其他临时目录仍独立；这不是直接把共享项目目录挂给用户命令。启动桥返回时，镜像已经创建，节点计量能看到其固定长度；导出器另持有输出及清单预留。启动失败且进程退出不明时，不提前释放尚可能被使用的镜像预留。

导出器按终端注册的文件系统类型和目录绑定读取，既兼容历史 tmpfs 描述符协议，又支持 ext4。新终端的完成条件仍然是进程退出、导出和文件事务发布分别有证据；镜像存在不构成发布成功。已有历史不重新标为成功。

底层只读打开要求调用者先核验旧拥有者及全部已记录执行范围退出，并额外核对镜像身份、UUID、独占锁和无存活 loop 挂载。只读挂载使用 ro/noload，禁止日志回放写盘。本阶段故障检查证明只读恢复前后整个镜像的哈希没有变化。磁盘工具的输出在管道读取时限制为 64 KiB，执行时间有上限；创建/挂载工具复用实际父进程死亡保护，避免启动门被杀时尚未进入载荷 PID namespace 的工具脱离拥有者。

## 实际验证

本轮 45 条实际基础设施检查，真实 LLM 调用 0、模拟 LLM 调用 0。没有执行历史模拟模型回归。

| 检查 | 证据 | 条数 |
|---|---|---|
| nobody 写满、实际 SIGKILL、只读恢复、目录/权限、拒绝已有镜像/错误 UUID | `tmp/sandbox-disk-checks-sb2bywah/checks.json` | 7 |
| 实际 FS/SQLite API 猝死，核验终端/导出进程退出后读回镜像 | `tmp/sandbox-owner-disk-gwdimzjj/checks.json` | 6 |
| 实际 S3/PostgreSQL API 猝死，额外核验未写入虚假成功状态 | `tmp/sandbox-owner-disk-rpihkykt/checks.json` | 7 |
| FS 强制关闭、新增/修改/删除文件发布 | `tmp/sandbox-export-owned-iwjezrro/checks.json` | 7 |
| FS 正常退出、新增/修改/删除文件发布 | `tmp/sandbox-export-owned-6ua98mh3/checks.json` | 7 |
| 真实 S3 强制关闭及文件发布 | `tmp/sandbox-export-owned-8rhh0w4d/checks.json` | 6 |
| 完整沙箱入口的零额度拒绝、原连接恢复、输出保存 | `tmp/session-capacity-1i09e8gd/checks.json` | 5 |

猝死测试通过真实产品 WebSocket 写入并读取随机值，直接杀死隔离 API，没有先发 exit 或 DELETE。之后核验 API、终端与导出进程的已记录 Windows/Linux 身份均退出，才使用只读镜像检查器读回文件、空目录和 0600 权限；没有重新执行用户命令。S3/PostgreSQL 的隔离库删除回执为 `tmp/mixedread-postgres-7x3ab6nk/cleanup.json`。开发数据库和端口 8000 未操作。

首次产品集成检查 `tmp/sandbox-export-owned-mputkids/checks.json` 失败：filesystem 字符串被 fstatvfs 结果覆盖，导致 ext4 描述符绑定缺字段。已修复变量遮蔽并复验，原失败保留。早期可行性实验为 `tmp/sandbox-disk-probe-cn7n4y0w/recovered.json`，不重复计入本轮 45 条。

## 恢复前的产品进程核验（2026-11-07 后续）

已增加 `GET /api/v1/terminal/sessions/{sid}/recovery-inspection`。它只给出当前进程证据，不授予恢复权限、不挂载镜像、不重放命令、不发布文件，也不改写原会话的完成状态。返回 `processes_exited` 只表示本次观察的已登记进程全部退出；响应同时明确 `image_verified=false`、`recovery_performed=false`。真正的恢复执行器必须重新核验，不能把这个 GET 的旧响应当作执行凭据。

入口要求当前用户仍是会话创建者和算例所属工作区的拥有者，用户及算例可用，原始文件来源和当前节点私有镜像路径一致。操作系统查询结束后再次读取授权和登记记录；记录在查询期间变化则拒绝给出退出结论。租约过期、当前 API 没有该会话、历史 stop 回执都不能替代实际进程身份观察。

核验包括旧 API 拥有者、终端 Windows/WSL 根、PTY gate、关联导出请求的拥有者及全部登记子进程。仍能发起工作的拥有者存活、出生身份不符、异机或无法观测、准备中的子进程缺失登记等均拒绝。已完成终端不再具有启动权限，因此其原 API 仍存活时，若实际子进程均退出，可以返回进程退出证据。没有创建虚构的退出码或 stop 回执。

导出记录同时按会话 ID 和私有镜像路径交叉查找，并纳入缺失绑定的异常记录，避免只损坏会话 ID 就把同一镜像的持有者漏掉。SQLite 和 PostgreSQL 使用各自的 JSON 查询语义；损坏 JSON 拒绝核验。每次最多处理 8 个关联请求、每请求 8 个子进程，超限明确拒绝；同一 API 最多并发两次核验，并设置查询期限。这是当前限制，不是任意历史规模的恢复支持。后续恢复执行仍须有独立持久登记、镜像身份及独占检查。

本次最终源码对应 16 条实际基础设施检查、12 条协议/故障检查；真实模型调用 0，模拟模型调用 0。没有运行历史模拟模型套件。

| 场景 | 实际基础设施 | 协议/故障 | 证据 |
|---|---:|---:|---|
| FS/SQLite：真实沙箱写读、API 猝死重启、实际进程退出及授权核验 | 6 | 6 | `tmp/sandbox-recovery-inspection-ta43s57x/checks.json` |
| S3/PostgreSQL：同样的真实故障路径及 JSON 绑定查询 | 6 | 6 | `tmp/sandbox-recovery-inspection-reh5l_gk/checks.json` |
| FS：正常关闭后原 API 仍存活，实际子进程均退出 | 4 | 0 | `tmp/sandbox-recovery-inspection-oraddlrl/checks.json` |

故障检查仅更改隔离数据库的所有权记录，并调用真实进程探测；不伪造模型行为或 OS 探测结果。覆盖缺失子进程登记、丢失/损坏导出绑定、会话 ID 改变但镜像相同、来源改变、存活导出拥有者、PID 相同但出生时间不符。临时 PostgreSQL 库删除回执：`tmp/mixedread-postgres-s8modwa3/cleanup.json`。

首次 `tmp/sandbox-recovery-inspection-47nryhdf/checks.json` 因测试过度要求输出 CRLF 而超时，保留失败；修订为输入中不存在的完整随机标记，只能由真实 shell 拼接并写读后产生。该失败运行的所有登记进程已实际确认退出，见同目录 `post-failure-exits.json`。交叉镜像绑定修订前的两组通过保留为历史证据，不重复计数。开发数据库及端口 8000 未操作。

本次验收索引：`tmp/mainline-sandbox-inspection-evidence/validation.json`；源码归档：`platform/backups/mainline-sandbox-inspection-20261107.zip`。任务数不变，完整恢复仍是下一步。

## 未完成工作

1. 恢复前的授权和实际进程核验 GET 已接入并验证；继续实现独立持久登记的恢复操作、只读镜像内容读取/下载、来源冲突处理及界面。进程核验和底层只读检查器均不等于完整恢复入口；恢复必须创建新操作，不得重放命令或窃取仍存活的执行。
2. WSL/机器重启与磁盘损坏、挂载/格式化中断、启动取消、导出器死亡、额度耗尽的完整恢复矩阵。当前文件身份包含设备/inode，跨 WSL 重启后的身份兼容性尚未证明；不能外推为所有机器故障的数据恢复保证。
3. 已发布镜像和失败镜像的有证据清理、来源已改变时的恢复冲突与选择；目前镜像仍保留并计入节点占用。
4. 空目录、目录/文件类型变化、权限元数据的正式文件事务发布，以及本地超过 256 个变更文件的批次。镜像能保留这些内容，不代表现有发布事务已支持它们。
5. 全局内存/并发、跨 worker 交错、WSL 目录外资源边界、全部原生 CLI 入口、长期每用户环境及 egress 等主线和完整交付要求继续有效。

主线 33/53 完成，剩 20 项；资源清单 11/17，剩 6 块；295 项全功能仍需最终逐项交付验收。本阶段没有关闭上述大任务。

源码归档：`platform/backups/mainline-sandbox-disk-20261107.zip`。验收索引：`tmp/mainline-sandbox-disk-evidence/validation.json`。当前后续任务：`tmp/sandbox-disk-pending.json`。
