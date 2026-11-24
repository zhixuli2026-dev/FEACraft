# 执行期间的磁盘用量监控

## 2026-11-19 RB-13 本地算例终端工作目录

`VIBEFLOW_TERMINAL_SESSION_WORKSPACE_LIMIT_MB` 默认为 `0`。设为正数后，绑定本地文件系统算例的普通 cmd 和非特权 WSL 会采样整个工作目录的保留字节，并按 `VIBEFLOW_NODE_STORAGE_MIN_FREE_MB` 检查目标卷余量。先在登记会话前检查，再在放行真正的 shell 前复查同一目录身份。运行期断线或挂接时沿用启动限额，目录字节超限与卷余量不足使用 `disk_limit`，无法核验采样使用 `disk_unverified`。会话关闭复用已注册的进程范围和持久停止凭据，保留用户文件。 [配置项](../server/.env.example) · [终端生命周期](../server/app/services/terminal_sessions.py)

在独立 SQLite 和 D 盘文件系统中，cmd 与实际 UID 65534 的 WSL 各验证两次初始拒绝。真实文件超过 1 MiB 或目录卷实际余量低于设置，均返回 `409` 且没有创建 NativeSession。低水位通过比较实际卷余量与测试配置触发，无需填盘。默认会话在配置随后启用限额时各写入 1,310,720 B，继续运行并能挂接。启用限额的会话在断线后各写到 1,310,720 B，收到 `disk_limit` 并保留现有文件与增长文件。另一个 cmd 真实创建目录条目，达到 168 项时超出 128 项采样预算，记录 `disk_unverified`，保留 262,144 B 文件。 [正式实际回执](../../tmp/terminal-workspace-budget-uio1mab4/terminal-workspace-budget.json) · [复验入口](../server/terminal_workspace_budget_acceptance.py)

在新 API 上读取三个受限会话的原尾部和停止凭据，WebSocket 回放同一资源原因。独立复查五个会话的根、门及三条实际脱离后代均退出。API PID 34200 和 28792 均已退出，55949 已关闭。首轮夹具先把全账号条目限制设为 128，既有账户级扫描实测 261 项而返回 503。复验在实际写入进程就绪并断开后才设定该采样上限。C 盘可用从 22.01 GiB 到 21.97 GiB，全部交接文件在 D 盘，Docker 构建缓存 0。真实与模拟模型调用均为 0。 [独立进程退出](../../tmp/terminal-workspace-budget-uio1mab4/independent-exit-verification.json) · [首轮失败及原因](../../tmp/terminal-workspace-budget-9khvnej_/fixture-account-entry-budget-diagnosis.json)

本节验证本地 FS 算例的周期采样，记录可能已超过限额时才停止的真实字节。无算例的终端、目录外写入和操作系统级硬配额属于独立边界。绑定 S3 算例的普通终端工作副本通过下节实测。 [已有 S3 下载准入](../server/app/services/staging_download.py) · [已核验停止后的同步](../server/app/routers/terminal.py)

## 2026-11-19 RB-13 S3 算例终端工作副本

同一启动限额用于 S3 普通 cmd 和非特权 WSL 的独占工作副本，默认仍为 `0`。节点低水位在下载前返回 `409`。节点准入通过后，初始快照超出终端目录限额也返回 `409`，本次 staging 和 manifest 随启动失败清理，会话及原生进程尚未登记。两类拒绝均经真实卷余量、实际远端文件和正常 WebSocket 回执验证。 [终端工作目录采样](../server/app/services/terminal_sessions.py) · [S3 下载准入](../server/app/services/staging_download.py) · [正式实际回执](../../tmp/s3-terminal-workspace-budget-pw5h702y/terminal-s3-workspace-budget.json)

独立 SQLite 与开启身份认证的真实 SeaweedFS 返回匿名 `403`、签名 `200`。一个 cmd 和一个 UID 65534 的 WSL 在 WebSocket 断开后各将文件写到 1,310,720 B，跨过 1 MiB 上限并收到 `disk_limit`。已验证的进程范围清空后，普通同步链路各提交一个文件。公开 `/raw` 下载返回精确字节，两个本地工作副本及 manifest 随后移除。API 冷启动后，三条实际终态及同一对象字节仍可读取，WebSocket 回放相同停止原因。 [测试入口](../server/terminal_s3_workspace_budget_acceptance.py) · [普通同步](../server/app/services/staging_sync.py) · [实际同步见证](../../tmp/s3-terminal-workspace-budget-pw5h702y/actual-staging-sync.jsonl)

第三个真实 cmd 断开期间，另一个普通文件上传先提交同路径。停止核验后同步因内容前置条件冲突而退出，远端原件仍可下载，本地 1,310,720 B 及原 manifest 保留。运维可按原会话记录中的 `staging_prefix`、`source_prefix` 与所有者定位本地工作副本，核对 manifest 和实际文件哈希，再以当前写权限和目标版本通过普通文件接口保存。实际停止凭据、远端内容哈希和本地保留路径见独立复查。 [实际冲突与退出](../../tmp/s3-terminal-workspace-budget-pw5h702y/independent-exit-verification.json)

测试退出码为 0。独立核对三个会话的根、门与脱离后代均已退出。API PID 6768 与 36420、SeaweedFS Linux PID 503502 均已退出，61653 和 24382 关闭，随机隔离身份文件已回收。模型调用 0，C 盘 21.96 GiB 前后相同，Docker 构建缓存 0 B。 [SeaweedFS 清理回执](../../tmp/s3-attempt-store-wgt6ddut/seaweed-exit.json)

## 2026-11-19 RB-17 工作目录之外的写入边界

SQLite、PostgreSQL 与文件系统、SeaweedFS 的四种部署共执行 12 个真实终端和 8 个实际 WSL 沙箱作业。普通 cmd 和非特权 WSL 在各自的 C 盘和 D 盘测试位置写入标记，非特权 WSL 也写入自己的 Linux 临时目录。两种隔离模式在同一批宿主路径都返回 `ENOENT`。已通过独立挂载设备号核对 C、D 和 Linux 根卷。[SQLite 与文件系统](../../tmp/execution-write-boundary-48m6mugu/write-boundary.json) · [PostgreSQL 与文件系统](../../tmp/execution-write-boundary-u2snkm4y/write-boundary.json) · [SQLite 与 S3](../../tmp/execution-write-boundary-8t7qcyiv/write-boundary.json) · [PostgreSQL 与 S3](../../tmp/execution-write-boundary-7k3osvww/write-boundary.json)

| 运行方式 | 实测边界 |
| --- | --- |
| 普通 cmd、普通 WSL | 依 Windows 或 WSL 宿主权限访问其可见路径。普通 WSL 的实际 UID 为 65534。 |
| 持久沙箱终端 | `/case` 是独立的固定 128 MiB ext4 镜像，`/tmp` 有独立的 128 MiB tmpfs。`/output` 的实际可写层属于 512 MiB 沙箱根 tmpfs。原有真实写满镜像的 `ENOSPC` 回执补证固定镜像边界。 |
| 旧沙箱作业 | `/case` 位于 `/tmp` 的同一卷，`/output` 是本次作业的独立宿主目录挂载，受配置的工作目录用量采样。 |

四组旧沙箱作业各写入 1,310,720 字节，跨过验收配置的 1,048,576 字节预算后退出为 `resource_limit` / `disk_limit`，保存实际峰值并核验进程范围已清空。该限制在采样点停止进程，写入发生于停止之前。所有隔离 API、临时库和对象存储已清理，模型调用和模拟调用均为 0。RB-17 的原验收条件覆盖实际路径、卷和拒绝行为，按上述配置边界验收通过。[复跑入口](../server/live_llm_execution_write_boundary_acceptance.py) · [独立 ext4 写满回执](../../tmp/sandbox-disk-checks-sb2bywah/checks.json)

## 既有作业监控

后续更新：prepared/prebundle重启恢复现已接入冻结额度的Watch，并保留复制到提交期间的违规；新节点只报告本节点观测。详见[恢复磁盘监控](recovery-storage-monitoring.md)。

2026-11-06，N4-05 增量。主线仍为 33/53 完成、剩余 20 项。承接[节点缓存准入](node-storage-admission.md)，本轮补上运行中的目录增长检查。

## 实现与边界

新尝试在创建时将 `storage_policy` 写入不可变输入描述，包含目录字节上限和磁盘最小可用空间；显式重试继承原策略，后续配置变化不能提高已提交尝试的额度。默认目录上限为 4096 MiB，沿用节点最小可用空间 256 MiB；配置为 `VIBEFLOW_EXECUTION_WORKSPACE_LIMIT_MB`、`VIBEFLOW_NODE_STORAGE_MIN_FREE_MB`。

`execution_storage.Watch` 在原生负载释放前检查初始用量，执行期间后台检查，并在收尾及终态提交时再次检查。封存作业监控整个尝试目录（inputs、run、同目录内临时文件），旧作业监控其工作目录。报告 worker 与 finalizer 在同一受监控目录下的写入也计入。默认采样间隔为 0.25 秒，可通过 `VIBEFLOW_EXECUTION_STORAGE_POLL_SECONDS` 调整。

目录盘点在线程执行，沿用时间和条目数限制。根目录身份变化、链接或无法确认的盘点会触发 `disk_unverified`。正常工具删除临时路径可能使盘点遇到 FileNotFoundError，因此在同一时间/条目预算内最多进行三次完整盘点；不能确认时不按零用量继续。取消监控时等待盘点线程结束。

目录超额或磁盘余量不足触发 `disk_limit`。它们沿用整次执行预算事件，停止所属主进程及原生子进程，终态使用 `resource_limit`。进程停止协议和停止证据校验共用原因集合，仍要求真实退出和进程组清理证明。失败产物保留，不通过删除文件制造低用量。

终态事务记录 `attempt.storage_usage`：冻结的策略、受监控相对目录、成功采样次数、当前字节、已观测峰值、已观测最小磁盘余量及测量状态。无法测量时 `current_bytes` 为 null，保留已知峰值，不把未知值写成零。已有 `attempt.resource_usage` 记录磁盘违规原因，恢复输出证明也识别这两类失败。

**这是周期检测与停止，不是 OS 磁盘硬配额。** 快速写入可在两次检测之间超额；文件字节不等于磁盘块占用；采样峰值也不等于所有瞬间的真实峰值。没有为原生进程声明未来空间预留。磁盘余量只针对受监控目录所在卷，不能据此声称约束任意 shell 在其他磁盘、WSL 根文件系统或目录外的写入。

## 验收

`execution_storage_smoke.py` 分两批在 SQLite/PostgreSQL 各覆盖 10 个场景：正常完成、快速写入并退出、运行中超额、提交后配置提高、收尾回调超额、报告 worker 超额、报告 finalizer 超额、真实 WSL 超额、测量不可确认、重试继承策略。检查真实进程退出、终态原因、持久用量及失败发布状态。

首轮快速写入发现停止原因白名单缺少磁盘类别，已统一停止端和证据端的原因集合。原报告兼容检查发现瞬时删除目录误报，已增加有界盘点重试。WSL 测试最初误将协议状态写作 empty，实际协议为 `native_state=exited` 且 `scope_empty=true`；修正测试后保持严格退出校验。

原日志预算、整次超时、恢复和 S3 执行兼容检查的具体范围与回执见 `../../tmp/mainline-execution-storage-evidence/validation.json`。所有数据库测试使用隔离临时库；没有修改开发数据库或重启开发 API。

## 后续 N4-05 单元

1. 产物复制、远程归档和旧 staging 的分配前峰值预算，避免只在复制发生后发现超额。
2. API/worker 重启后的磁盘监控恢复与空间占用绑定。目前新策略会被加载，但既有发布恢复流程还没有恢复本轮 Watch；本次恢复兼容检查不能替代磁盘恢复故障验收。没有策略的历史尝试也没有新监控证明。
3. 独立 native request、CLI、交互运行等其他生命周期入口，以及目录外写入的 sandbox/OS 限制。
4. 长 I/O 与数据库锁/心跳、未知用量、跨节点并发和安全缓存淘汰。全部满足主线验收后才关闭 N4-05，完整 53 项与 295 项范围不变。
