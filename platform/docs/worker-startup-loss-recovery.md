# 启动登记期间的 Linux worker 失联恢复

2026-11-16。主线仍为 48/53，剩余 5 项；N4-07 尚未关闭。全功能 295 项的交付目标和状态保持不变。

## 问题与修复

实际 Linux worker 已保存 namespace 和 launch preparation，但尚未提交 running 身份时硬退出，旧实现只写 `registration_recovery_required`。即使原 worker、gate 和 namespace init 都已退出，任务仍停在 starting，后续恢复不能完成。

复用原来的准备事件、身份观察链、namespace 退出证明和 v4 排空凭据：新 worker 核对原 claim、输入、准备记录及完整范围，确认原 worker/gate/init 已退出后，将原 attempt 收为 interrupted，并解除已结束执行的阻塞。新增 `vibeflow.job-linux-startup-recovery.v1` 区分该窗口；运行中恢复仍使用原有凭据格式。

原 `process_identity_json` 继续为 `{}`，缺失的原停止记录仍为空，不补造 owner 回调、不重跑原命令。锁定 case 后重新核对快照、准备事件和取消状态，避免观察期间状态变化造成错误收尾。已验证的排空凭据也不会再次被缺失登记检查归为未恢复。

## 真实验收

入口：`platform/server/live_llm_remote_startup_loss_acceptance.py`。生产函数先真实保存准备记录，再在该窗口执行 `os._exit(17)`；Windows API、Linux worker、Redis、PostgreSQL、S3 均为实际环境。模型使用本地授权 profile 26 的官方 DeepSeek `deepseek-flash`，不使用模拟模型。

最终证据 `tmp/remote-worker-9bvaskgq/`：

- 旧 owner 尚存时没有提前恢复；硬退出后独立观察到原 worker、gate、namespace init 退出。
- 原任务 interrupted；原 attempt、worker 身份、缺失 running 身份、子执行记录和输入清单保留。只保留一个 attempt，恢复事件明确 `replayed: false`。
- 原命令若执行会创建标记，验收确认标记没有出现；既有发布文件不变。随后新任务成功执行，真实模型使用文件工具读回新随机证明。
- 启动失联及远端基线共 6 个实际任务、2 次真实模型 HTTP 200。独立事后核对 15 个原进程身份均不再存活。
- 对实际恢复凭据副本的 12 种篡改均被拒绝；这是纯凭据校验，不是模型测试，也不修改数据库。

兼容验收沿用显式真实入口：`live_llm_worker_shutdown_acceptance.py`（Windows/WSL 关闭、取消和重启）及 `live_llm_remote_namespace_acceptance.py`（原 Linux 运行中失联）。最终统计与完整证据散列见 `tmp/mainline-N4-07-startup-loss-evidence/validation.json`。

## 保留失败证据

`tmp/remote-worker-pitggfhf/` 实际复现修复前故障。第一次修复在 `tmp/remote-worker-qd61sgqx/` 仍失败：顶层 Job 在启动窗口实际为 queued，而 attempt 为 starting，过窄的 running 条件阻止恢复。最终修正为允许 queued/running，但仍严格要求 starting、完整准备证据、无取消和无输出。两次失败均未调用模型，没有回退模拟；其原进程、Redis/S3、临时数据库与临时数据库访问规则均已核验清理。

## 剩余范围

上一轮只覆盖根执行完整准备已落库、running 登记尚未提交的实际窗口。后续补齐下述两个相邻缺口；更早连 namespace 身份也未保存、专业 child 登记未完成及其他外部 owner 仍须按原计划核对，不能用本结果外推。资源预算、全故障矩阵、发布文档和最终全功能验收仍在剩余 5 项主线中。

## 后续增量：namespace 已保存与取消待收尾

`live_llm_remote_startup_boundaries_acceptance.py` 复用同一恢复协议，验证三个真实窗口。实际证据为 `tmp/remote-worker-d0_88qk7/`：

| 窗口 | 原始事实 | 恢复结果 |
|---|---|---|
| namespace 保存后、launch_prepared 之前 owner exit 17 | launch_prepared 事件数为 0，running 身份为空 | interrupted；缺失事件和身份保持原样；显式新任务成功 |
| 同一早期窗口 owner exit 17 后、恢复前提交 API 取消 | Job 为 cancelled、attempt 为 cancelling，原取消时间已持久化 | cancelled/cancel_requested；取消时间绑定到恢复凭据；显式新任务成功 |
| 已保存 launch_prepared 的上一轮窗口 | 准备事件数为 1，running 身份为空 | interrupted；保留原有恢复行为；显式新任务成功 |

新增两处实际能力修复，第三个窗口属于兼容验证。第二项明确是在原 owner 已退出后、新 worker 尚未恢复时提交取消，不宣称覆盖所有取消竞态。三个窗口均实际创建原生进程并退出原 worker，没有合成模型回复、进程身份或停止结果。旧命令的标记始终未创建，旧文件保持，原 attempt 不重放。

实现仍要求完整 namespace 身份、原启动意图、创建/宿主握手观察链，以及原 worker、gate、init 的实际退出。`launch_prepared` 缺失时引用原 namespace 准备事件中的完整身份，保留 `prepared: null`；取消路径保留 `original_phase: cancelling` 及原取消时间，收尾到 cancelled。事务重新加锁后复核原状态、取消时间和事件；凭据验证同时检查它们。缺少 namespace、身份不符或原实例仍活跃时，不能借此获得排空证明。

三个实际凭据共 37 项副本篡改拒绝检查，另列为纯协议验证；取消凭据增加取消时间篡改拒绝。完整启动基线、Windows/WSL 关闭兼容、Linux 运行失联兼容及真实模型的最终统计与退出核验见 `tmp/mainline-N4-07-startup-boundaries-evidence/validation.json`。该记录不把兼容重跑算作新增功能。

接下来优先推进原计划中带确认记录的求解后处理及报告完整流程；未覆盖的登记故障保留在原清单，不新增权限层和阶段退出条件。
