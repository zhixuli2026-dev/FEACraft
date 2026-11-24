# 镜像清理启动证据与双库取消验收

2026-11-07。已为镜像清理请求接入持久启动观察，补齐部分“进程已启动、完整身份尚未登记”的恢复路径，并完成 SQLite/PostgreSQL 的双 API 竞争、连续取消和接管重试验证。主线保持 33/53 完成、20 项未完成；资源边界 11/17 完成、6 块未完成；295 项全功能最终验收目标不变。

后续已修复准备取消后 API 仍存活造成的误阻塞，以及实际停止后遗留的节点预留，双库证据见[准备取消重试与预留释放](sandbox-image-live-owner.md)。出生身份完全缺失或未知 WSL 身份仍未放行。

## 本次实现

`sandbox_image_launch.py` 在实际启动前提交 `terminal.image_launch_intended`，绑定请求、子进程预留、父启动标识、目的、参数哈希、分发环境及 gate/probe 代码哈希。实际 `OwnedProcess` 在 `created`、`host_ready`、`native_ready` 阶段通过回调提交 `terminal.image_launch_observed`，记录 OS 读取的出生身份；每个事件 ID 固定，最多三条观察，缺失、重排和绑定不符均拒绝。

完整子进程登记前必须核对最后的启动观察与实际进程记录一致。清理执行者死亡后，独立对账可使用这些观察补足原始子进程日志中缺失的身份，但不修改该日志，也不伪造正常登记或 stop 回执。对账事件绑定完整观察链哈希，完成查询和来源准入会重新校验。

目前可恢复两种状态：

- 最后观察是 `created`：实际宿主出生身份已经持久保存。按当前启动协议，`host_ready` 必须提交后才会第一次放行宿主创建 WSL gate，因此没有 `host_ready` 记录时，只需核验已知宿主及 API 所有者确实退出。
- 最后观察是 `native_ready`：宿主与 WSL gate 的实际出生身份都已保存，核验全部退出后允许新的登记清理。新清理还会将旧 gate 纳入原生退出核验集合。

如果完全没有出生观察，或者只有 `host_ready` 而没有 WSL 出生身份，仍拒绝自动对账。不能用 API 退出、等待一段时间、没有正常停止日志或租约过期替代缺失作用域证据。本次没有关闭这些窗口。

## 实际基础设施验收

| 环境与算例 | 已验证内容 | 证据 |
| --- | --- | --- |
| SQLite，双 API | 实际 gate 活动时拒绝另一个 API；连续取消三次期间仍保留执行；实际停止后镜像仍在；另一个 API 重试删除；两端返回同一完成回执、快照可下载。5 项检查。 | [xix8fqjq](../../tmp/sandbox-recovery-ui-retirement-xix8fqjq/image-concurrency-checks.json) |
| PostgreSQL，双 API | 相同 5 项检查，随后真实模型读回恢复文件。使用独立临时数据库。 | [ojqqy0bj](../../tmp/sandbox-recovery-ui-retirement-ojqqy0bj/image-concurrency-checks.json) |
| 最终启动观察代码，PostgreSQL | 新观察接入后再次执行上述 5 项，验证附加登记事务未破坏取消和互斥。临时数据库已删除。 | [ap8wn_v7](../../tmp/sandbox-recovery-ui-retirement-ap8wn_v7/image-concurrency-checks.json)、[清理记录](../../tmp/image-postgres-wnwpxqvx/result.json) |
| SQLite，启动中 API 退出 | 在真实 `created`、`native_ready` 观察提交后各终止 API 一次；未登记日志保持原样；通过独立退出证明重试成功。3 项基础设施检查、1 项观察哈希篡改故障检查。 | [brdjkae2](../../tmp/sandbox-recovery-ui-retirement-brdjkae2/image-preparation-checks.json) |
| SQLite，身份缺失拒绝 | `host_ready` 后真实退出，确认缺失 WSL 身份时仍返回 409，原镜像、快照和请求日志不改。2 项检查。 | [d6tziyp_](../../tmp/sandbox-recovery-ui-retirement-d6tziyp_/image-preparation-checks.json) |

双 API 测试在真实进程完成登记后、最终放行前设置等待点，并对实际 API 任务发出三次取消。取消请求最终返回 500/连接终止属于该故障场景，成功依据是实际子进程停止及后续明确重试，不能把故障请求算作正常完成。启动中退出使用真正的 `os._exit`；SQL 观察篡改另算协议故障，不算真实进程证据。

五次运行的完整登记进程分别为 11、11、9、7、11 个，均确认退出；启动故障中尚未进入完整日志的宿主/WSL 身份另由 `image-preparation-checks.json` 保存实际退出观察。三个第二 API 进程已退出，八个测试端口关闭；两次 PostgreSQL 临时数据库均已删除。开发数据库未修改或迁移。

## 真实模型验收

- [PostgreSQL 取消与重试后](../../tmp/sandbox-recovery-ui-retirement-ojqqy0bj/live-result.json)：2 次真实调用，8505 tokens。
- [启动身份对账与镜像删除后](../../tmp/sandbox-recovery-ui-retirement-brdjkae2/live-result.json)：2 次真实调用，8587 tokens。

合计 **4 次真实 DeepSeek 调用，17092 tokens，模拟调用 0**。服务 `api.deepseek.com`，模型 `deepseek-flash`。两次均由模型选择 `read_text_file` 读出从保留快照恢复的随机文件，并将真实内容写入回复和会话记录。严格只返回随机字符串仍含额外说明，未通过这一格式要求。浏览器、schema 或基础设施检查不计入模型调用。

## 后续工作

1. 无任何持久出生观察、`host_ready` 到 `native_ready` 之间的未知 WSL 身份，以及准备阶段取消后存活 API 所有者的安全恢复；不能通过放松退出证明解决。
2. PostgreSQL 的启动中断对账和独立事件提交前后故障尚未覆盖；本轮 PG 已覆盖正常登记、实际取消和双 API 接管，不能外推到全部恢复事务。
3. 原生 unlink/回执写入定点故障、权限变更、WSL/主机重启与身份变化、空间耗尽等专项仍需完成。
4. 原镜像之外的导出暂存、失败恢复副本、长期快照和额度结算，以及完整恢复语义、RB-12 至 RB-17 和所有剩余主线/全功能要求继续。

实现源码：`sandbox_image_launch.py`、`native_request_execution.py`、`native_request_records.py`、`sandbox_image_reconciliation.py`、`sandbox_image_cleanup.py`。未变更 UI 或数据库 schema。回归登记仅批准本轮明确审查的无模型算例；真实模型分别通过 `live_llm_image_concurrency_acceptance.py --postgres` 和 `live_llm_image_preparation_acceptance.py` 进入。
