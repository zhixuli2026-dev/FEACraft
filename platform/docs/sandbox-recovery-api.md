# 沙箱只读恢复快照 API

2026-11-07。已实现独立持久登记的恢复操作、恢复记录查询和 ZIP 下载。真实 API 猝死后，在确认原拥有者及进程退出的前提下，从保留的 ext4 镜像读回文件、空目录和 Unix 权限元数据。不会重放原命令，不直接覆盖项目文件，也不会把原终端改写为已成功发布。

这是恢复功能的一部分：界面、确认写回项目时的来源冲突处理、旧会话及导出记录的收尾、保留镜像/快照清理、完整取消/重启故障矩阵仍待实现。主线及全功能总目标保持有效。

## 接口

所有接口使用现有登录认证，要求用户仍是会话创建者、仍拥有该算例所在工作区，用户和算例均可用。

| 方法与路径 | 行为 |
|---|---|
| GET /api/v1/terminal/sessions/{sid}/recovery-inspection | 上一阶段的进程核验；响应仅供查看，不能作为恢复执行凭据 |
| POST /api/v1/terminal/sessions/{sid}/recoveries | 创建独立恢复请求，等待受控快照完成后返回 201、操作 ID、清单和完成回执 |
| GET /api/v1/terminal/sessions/{sid}/recoveries | 分页查询成功、失败或未完成的恢复请求；每页最多 50 条，以 next_before 继续 |
| GET /api/v1/terminal/sessions/{sid}/recoveries/{id} | 查询持久状态；成功操作额外核验完成回执与清单 |
| GET /api/v1/terminal/sessions/{sid}/recoveries/{id}/download | 校验数据库回执和整个归档哈希，再次核对权限后，从同一文件句柄有界流式下载 |

恢复请求不接受任意磁盘路径、命令、镜像标识或文件选择。来源从原终端的已登记配置推导。存活/不明拥有者、来源变化等返回 409；真实恢复执行失败返回 502，并保留原镜像与失败操作。未完成、损坏或无权限的归档不能下载。连接中断后可以通过恢复记录列表查找操作，不依赖 POST 响应是否被客户端收到。

## 执行与证据

1. 重新核验原 API 拥有者、终端 Windows/WSL 根、PTY gate、导出请求拥有者和全部已登记子进程；重新检查授权及数据库记录指纹。租约过期不是进程退出证据。
2. 预留节点容量，建立私有 .terminal-recovery 目录，创建 terminal.recover 类型 NativeRequest。固定 sandbox_recover 调用只能登记一个子执行，不能重复执行或调用其他原生用途。
3. 实际 Windows/WSL 根登记后，在释放启动前再做进程核验；最终释放事务再次核对授权、原始配置和指纹。
4. 受信任的外层启动门在原 PID 命名空间内检查 Linux 出生身份均已退出，再验证镜像设备/inode/长度、文件系统 UUID、独占锁和无既有 loop 挂载。镜像按 ro/noload 挂载，禁止写盘及日志回放。
5. 只读目录描述符交给固定恢复复制器。耗时哈希、扫描、复制和归档运行在受监督的独立 PID/mount 命名空间中，允许停止整组进程。该固定复制器保留读取 sandbox UID 私有文件的权限；不执行镜像内的程序或用户命令，普通终端/导出器的用户命名空间行为保持原有规则。
6. 生成文件清单、空目录及权限元数据，复制文件并制作 ZIP。原镜像恢复前后哈希必须相同；ZIP 内容与清单文件哈希逐一匹配。特殊文件、链接、目录越界、路径碰撞和超限内容拒绝生成成功回执。
7. 原生进程清理有完整 stop 证据后，才把清单/归档/原镜像哈希及归档长度写入数据库 result_json，并将新恢复请求置为 succeeded。旧终端和旧导出请求不被伪造为 closed/succeeded。

原镜像固定 128 MiB。恢复快照最多 128 MiB 文件、10,000 个条目、32 层目录；清单最多 4 MiB，ZIP 最多 136 MiB。节点预留覆盖文件副本、归档和控制元数据；一个 API 实例最多同时进行两次恢复。清理状态未知时保留容量预留，失败 HTTP 响应本身不代表 I/O 已退出。镜像、恢复文件和归档目前保留并计入节点占用，尚无自动清理。

下载前核验归档哈希和长度，核验后刷新权限及完成回执。传输从保留的同一文件句柄读取，每块最多 1 MiB、总长度受回执约束；响应使用 private/no-store。完成回执或归档损坏会拒绝读取，不回退为“仍然成功”。

## 数据库

新增迁移 a4e9bc5280cd，前序为 f3d8fa4179bc：

- native_requests.kind 增加 terminal.recover。
- native_requests.result_json 保存恢复完成回执，默认空对象。
- 存在恢复历史或非空结果证据时拒绝降级，避免静默丢弃记录。

已在隔离 SQLite 和临时 PostgreSQL 环境验证。开发数据库没有执行迁移，端口 8000 未操作；部署这版代码前需按项目现有流程运行迁移。

## 实际验收

最终证据及源码绑定见 tmp/mainline-sandbox-recovery-evidence/validation.json。本轮计数分为实际基础设施、协议/故障和模型调用；没有运行历史模拟模型回归。

| 场景 | 实际基础设施 | 协议/故障 | 证据 |
|---|---:|---:|---|
| FS/SQLite 猝死、恢复、再次重启及下载 | 13 | 3 | tmp/sandbox-recovery-inspection-e0927c7y/checks.json |
| S3/PostgreSQL 同样的恢复和失败路径 | 13 | 3 | tmp/sandbox-recovery-inspection-vv9060py/checks.json |
| 原终端强制关闭，新增/修改/删除文件正常发布 | 7 | 0 | tmp/sandbox-export-owned-ti4bn856/checks.json |
| 隔离 SQLite 升级、元数据、降级与历史保留 | 0 | 3 | tmp/sandbox-recovery-migration-lagdkpmb/checks.json |

合计 33 条实际基础设施、9 条协议/故障，真实 LLM 0、模拟 LLM 0。临时 PostgreSQL 库删除回执为 tmp/mixedread-postgres-za8jrk57/cleanup.json。此前通过的增量运行保留为历史，不重复计数；最终报告已将继承自“仅核验”测试的 recovery_performed 标记修正为实际恢复成功后才为 true。

真实场景经过产品 WebSocket 写入随机内容，直接杀死 API，重启后通过产品恢复 API 生成并下载 ZIP，再次重启验证记录和下载仍相同。读取实际包内字节、空目录和 0600 权限；检查原镜像哈希不变、原项目文件没有被发布、原终端没有被伪造为完成。

失败场景包括：存活会话及无权限请求在新执行登记前拒绝；替换隔离测试镜像回执中的 UUID 后，真实 blkid 核验失败，新请求记为失败、实际执行已清理、容量预留释放、无成功回执且不能下载；归档和数据库回执篡改分别拒绝。故障记录只作用于隔离环境，不模拟模型或 OS 探测响应。

另复验原有强制关闭终端的新增/修改/删除文件发布路径，避免新增恢复执行破坏正常终端收尾。迁移检查独立计为纯协议检查，不能当作运行恢复能力证明。

## 后续

1. 恢复记录与下载界面已验收，见[恢复界面与验收](sandbox-recovery-ui.md)。后续已接入[选中文件内容写回](sandbox-recovery-restore.md)，包括当前目标预览、前提变化拒绝和持久确认回执；完整目录/权限/删除恢复及三方冲突处理仍待。
2. 有证据地收尾原终端/导出记录，处理它们仍影响算例验证和清理的情况；新快照成功不等于这些旧执行已完成。
3. 恢复过程取消、API/WSL/机器重启、磁盘损坏、挂载与格式化中断、写满、超大目录、跨 worker 和清理状态未知的完整矩阵。当前 inode/设备身份跨 WSL 重启兼容性仍未证明。
4. 原镜像、成功/失败快照的有证据清理与容量回收；清理原镜像后的既有下载兼容性仍需实现。
5. 项目正式发布空目录、目录/文件类型变化、执行权限和超过 256 个变更文件的事务；ZIP 能保存不代表正式发布已具备。
6. 其余原生 CLI 容量边界、RB-13 至 RB-17，以及全部剩余主线和全功能交付项。

主线 33/53 完成，剩 20；资源清单 11/17，剩 6；295 项全功能仍需最终逐项验收。本阶段不关闭这些大任务。

## 2026-11-19 恢复 ZIP 的下载准入与排空

RB-14 补齐正式恢复 ZIP 下载的并发准入与请求期限。同一存储根复用既有归档准入，从权限核对到响应结束共享现有 300 秒期限。ZIP 校验仍有原 30 秒上限，并取调用请求的剩余时间。完成 SHA-256 校验后刷新权限和原提交回执，从同一打开句柄发送。工作线程在实际开始下一块读取前检查同一期限，已开始的读取排空后才关闭句柄及释放节点准入。ZIP 文件名、长度、私有缓存和证据异常的 409 保持原接口语义。[归档准入](../server/app/archive_admission.py) · [正式下载入口](../server/app/routers/terminal.py)

旧版从实际 WSL 快照生成 6,296,230 B 原生恢复 ZIP。首个真实 GET 停在实际读到第一块时，第二个 GET 也打开真正的档案文件，完成八块校验读取和七块响应读取，返回完整 HTTP 200。夹具仅对指定请求设置既有归档准入的 4 秒观察期限，原路由未继承该期限，4.219 秒后仍发送相同 ZIP。实际原生回执证明源文件和恢复成功，两代隔离 API 的 PID 与监听端口均独立确认退出。精确回归 1/1 证明旧版可复现该现象。[原版实测](../../tmp/recovery-download-admission-_y9s8x7z/recovery-download-admission.json) · [原版执行摘要](../../tmp/vibeflow-regression-all-20261119-170856-2acb30/summary.json) · [原 API 独立退出](../../tmp/recovery-download-admission-_y9s8x7z/independent-api-exit.json)

首次固定轮已实际复现过期时仍持有节点准入、第二请求 429 无文件打开，以及工作线程排空后 504。响应发送用例的原夹具只允许一次文件读取，实际 Starlette 在首块交到外层发送时已为下次发送预读一块。服务仅发送首块，连接中断且文件实际关闭。夹具随后按真实开始与返回时刻核对同一期限，并记录这一块有界预读。首次失败和两个 API 独立退出保留。[首次固定记录](../../tmp/recovery-download-admission-zkgro4mf/recovery-download-admission.json) · [独立退出](../../tmp/recovery-download-admission-zkgro4mf/independent-api-exit.json)

最终实际回归 1/1，耗时 37.594 秒。首次校验占用时和首块已经过期仍在排空时，第二个真实 GET 均返回 `429 archive_busy`，含 `Retry-After: 2`，文件打开次数为零。过期请求完成一块 1,048,576 B 的校验读取后关句柄，返回 `504 archive_timeout`，ZIP 正文发送量为零。[最终回执](../../tmp/recovery-download-admission-x0oygzuf/recovery-download-admission.json) · [执行器摘要](../../tmp/vibeflow-regression-all-20261119-171834-294c89/summary.json)

响应首块已开始发送时，期限到达导致客户端收到传输中断。两次真实文件读取的开始和返回都早于该请求期限，分别约早 3.968 秒与 3.953 秒。实际客户端断线单独复验也只发送一块，文件关闭，节点准入可以复用。随后以原授权重新下载 6,296,230 B，SHA-256 与提交回执和本地保留归档均一致，无登录和其他所有者分别保持 401 与 404。[具体读取时刻和 HTTP 回执](../../tmp/recovery-download-admission-x0oygzuf/recovery-download-admission.json)

只读 SQLite 与归档复核确认一条真实成功的原生恢复、该原生子执行已核验停止、原终端的七个登记进程身份均已观察退出。两代隔离 API 的 PID 34352、28208 以及端口 54759 已消失。模型调用 0 次。最终固定轮前后 C 盘 21.41→21.47 GiB、D 盘 382.73→382.59 GiB，11 个项目 Docker 容器保持运行。范围为实际 SQLite、WSL 和本地保留归档的正式下载生命周期。[保留档案独立核验](../../tmp/recovery-download-admission-x0oygzuf/independent-retained-receipt.json) · [两代 API 退出](../../tmp/recovery-download-admission-x0oygzuf/independent-api-exit.json)
