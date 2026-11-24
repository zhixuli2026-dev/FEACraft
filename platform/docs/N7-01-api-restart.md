# N7-01 排队与运行期间的 API 重启

2026-11-18，V13 和 V14 在单机 PostgreSQL、Redis、SeaweedFS 和独立 worker 部署通过。两个场景在同一个真实作业上连续执行。API、worker 使用各自的 PID 命名空间。本次仅更新隔离恢复部署中的程序，日常运行实例尚未部署本次修复。

| 场景 | 操作和判断依据 | 结果 |
|---|---|---|
| V13 排队后重启 | 停止 worker，从 HTTP 提交带封存输入的作业，再强制结束并启动 API。相同幂等键返回原作业。随后启动 worker。 | 作业 170 保留同一 attempt。事件 7 记录等待补投递，事件 8 首次认领，事件 15 进入运行。 |
| V14 运行时重启 | 子进程以独占创建方式写入输入摘要并等待释放。再次强制结束并启动 API，核对 worker 启动时间和作业进程身份后放行。 | 原 worker 继续执行。事件 17 进入收尾，27 记录停止，28 成功。认领、运行和成功转换各一次；日志摘要与源文件一致，最终执行范围为空。 |

第一次提交收到 HTTP 409，当次记录没有足够信息还原具体原因。第二次运行的作业 169 成功，但数据库事件 17 对活跃 worker 记录了身份不匹配。worker 自身记录的 PID 属于其容器，API 查询相同数字可能看到另一个进程，也可能找不到进程。现在先检查宿主、启动标识和 PID 命名空间。跨命名空间且租约有效时等待原 worker；租约到期且身份仍不可核验时保留待核验状态。作业 170 的数据库事件仅在排队时出现一次恢复提示。过期租约的跨容器分支尚未在本轮注入故障。

成功运行的标识为 case 34、job 170、attempt `3fda5e8f-da43-4b22-ac58-f06397754f9a`。本批只运行实际基础设施与故障注入，真实模型和模拟模型调用均为零。物理多主机及其他故障场景继续由 N7-01 跟踪。

- [首次提交记录](../../tmp/queued-restart-jbdp2nbz/result.json)、[修复前运行](../../tmp/queued-restart-78gd_f65/result.json)
- [最终运行与 API 生命周期](../../tmp/queued-restart-qgn5m07x/result.json)、[重启后运行状态](../../tmp/queued-restart-qgn5m07x/after-running-restart.json)、[数据库事件](../../tmp/queued-restart-qgn5m07x/events.json)、[终态](../../tmp/queued-restart-qgn5m07x/terminal.json)
- [本地投递双库历史结果](../../tmp/mainline-N5-delivery-evidence/local-result.json)、[Redis 投递双库历史结果](../../tmp/mainline-N5-delivery-evidence/redis-result.json)、[复验入口](../server/queued_restart_acceptance.py)

## 11 月 18 日并发提交、编辑与阶段确认

| 原故障场景 | 实际操作及结果 |
| --- | --- |
| V01 双击提交及同键重放 | 向普通作业端点同时发送两个同键请求，均返回作业 174。排队和运行期间分别重启 API，独占执行标记、日志和数据库事件均对应一次执行。另向工作流端点并发提交几何导出，两个响应及完成后的重放均指向同一流程。原流程经明确确认成功，只有一项几何尝试和一个发布版本，从 S3 读回 15,440 字节的 STEP 文件。 |
| V02 两个编辑器保存同一版本 | 原部署在一个写入成功后，对另一个请求返回了 `storage_quota_busy`。当前代码让带版本或摘要条件的编辑在进入算例写锁前限时等待存储准入，随后在锁内检查条件。复验中一个请求提交版本 1，另一个收到 HTTP 409、`revision_conflict` 和当前版本 1。S3 字节、成功回执、PostgreSQL 版本以及同一条修订审计的摘要一致。 |
| V07 工程阶段等待确认时版本改变 | 在几何导出已经发布、等待 B 阶段确认时执行上述编辑。旧确认收到 HTTP 409，算例仍在 A 阶段。显式刷新确认版本并再次确认后进入 B 阶段，流程成功，仍引用同一份几何发布。 |

V01 的浏览器双击及丢失回执后刷新已在前一阶段验证。本轮并发由两个 HTTP 请求触发，未重跑浏览器。V02 与 V07 的新证据来自同一隔离算例的连续操作。V02 修复应用于隔离恢复环境的 API，日常部署尚未更新。该组只验证实际基础设施和程序故障处理，模型调用为零。

- [普通作业并发](../../tmp/queued-restart-knj3tsww/concurrent-job.json)、[重启后的执行事件](../../tmp/queued-restart-knj3tsww/events.json)、[独立几何导出](../../tmp/queued-restart-knj3tsww/concurrent-workflow.json)、[已有浏览器验收](N6-07-browser-lifecycle-progress.md)
- [修复前的额度冲突](../../tmp/queued-restart-patv7_qe/result.json)、[修复后同一次编辑、确认与 STEP](../../tmp/queued-restart-36p0ol5q/concurrent-workflow.json)、[同轮恢复结果](../../tmp/queued-restart-36p0ol5q/result.json)

在新的算例 43 上独立复验，PostgreSQL 中仅有作业 187。作业日志非空，停止回执确认进程范围已清空，修订审计哈希链完整，最终取得 15,440 字节的 STEP。隔离环境已停止，11 个日常容器仍在运行。[同轮数据库、审计、进程与文件证据](../../tmp/queued-restart-6qpkr2_5/concurrent-workflow.json)、[七项检查](../../tmp/queued-restart-6qpkr2_5/result.json)

## 11 月 19 日能力探测进程突然失去 API

四种数据库与存储组合分别在子进程启动后尚未登记、登记放行后实际结束 API，共 8 次。真实 Windows Python 和 WSL 探测使用原 30 秒租约。16 条原请求在自然到期后公开显示 `recovery_required`，原始请求和子进程登记字节保持一致，持久停止回执与结束时间仍为空。第二次冷启动读到同一批记录。另对已放行的 8 个真实进程独立核验退出，公开查询依然保留原停止证明的状态。

随后通过实际能力入口显式发起 8 次新探测，各产生新请求标识，全部完成并核验真正停止。另一账户只能读取自己的记录。临时 PostgreSQL、SeaweedFS 和 API 全部退出或删除。旧验收留下的未核验记录以只读方式复查。此批属于实际基础设施故障验收，真实和模拟模型调用均为 0。[复跑入口](../server/live_llm_runtime_probe_owner_loss_acceptance.py)

- [SQLite 与文件系统](../../tmp/runtime-probe-owner-loss-aettzkic/runtime-probe-owner-loss.json) · [PostgreSQL 与文件系统](../../tmp/runtime-probe-owner-loss-zw_anx8m/runtime-probe-owner-loss.json)
- [SQLite 与 S3](../../tmp/runtime-probe-owner-loss-syz4xb3y/runtime-probe-owner-loss.json) · [PostgreSQL 与 S3](../../tmp/runtime-probe-owner-loss-qk324pin/runtime-probe-owner-loss.json)

### 登记已提交、业务尚未放行时的突停

SQLite 与文件系统、PostgreSQL 与 SeaweedFS 各复验一轮。真实 Windows 和非特权 WSL 探测均已提交原生出生身份后，夹具暂缓登记回调返回。此时独立 HTTP 查询读到两个请求为 `active`，子记录为 `running`。该字段随身份登记提交。两轮均确认没有进入放行函数，业务入口的启动标记也没有出现。随后终止的是经创建来源和出生时间核验的隔离 API 进程。

两轮共四条原请求。独立观察确认原 Windows 进程、WSL 宿主侧进程及已登记的 Linux 进程均退出。重启后原 30 秒租约自然到期，四条原请求显示 `recovery_required`，原身份、子记录与结果字节保持，结束时间和持久停止回执仍为空。显式重新调用公开自检产生四个新请求，均成功且持有已核验的原生停止回执。另起冷 API 读回相同历史，另一账户读到自己的空记录。运行涉及真实模型 0 次、模拟模型 0 次。

两轮六个 API 已独立确认退出。真实 PostgreSQL 临时库从 `pg_database` 核对已删除，SeaweedFS 的原出生身份已从 WSL 核对退出。两轮在同一物理主机完成。跨物理宿主仍需第二个获授权的独立节点，以及供两节点访问的隔离 PostgreSQL、SeaweedFS 和 Redis。[SQLite 与文件系统](../../tmp/runtime-probe-owner-loss-fbu93v1m/runtime-probe-owner-loss.json) · [独立清理](../../tmp/runtime-probe-owner-loss-fbu93v1m/cleanup-independent.json) · [PostgreSQL 与 SeaweedFS](../../tmp/runtime-probe-owner-loss-5ntwg7g6/runtime-probe-owner-loss.json) · [独立清理](../../tmp/runtime-probe-owner-loss-5ntwg7g6/cleanup-independent.json) · [节点核对](../../tmp/n7-01-native-probe-registration-20261119/physical-host-inventory.json)

登记后的入口为 `platform/server/live_llm_runtime_probe_owner_loss_acceptance.py --only-after-registration`，PostgreSQL 与 SeaweedFS 组合附加 `--postgres --s3`。[执行前源文件和辅助代码散列](../../tmp/n7-01-native-probe-registration-20261119/source-review.json)
