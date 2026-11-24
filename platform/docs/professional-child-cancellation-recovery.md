# 专业子任务失联后的取消恢复

2026-11-16，N4-07 增量；承接运行中取消收尾实现，补齐此前未实测的专业 child 准备阶段组合。主线 53 项、全功能 295 项范围保持。

## 验证范围

复用 `job_registration_recovery` 的 cancelling 分支、原始 child 准备/入组记录、独立守护与 v4 范围证明，不改生产恢复逻辑。显式真实验收入口为 `platform/server/live_llm_child_cancelled_owner_acceptance.py`，审查登记为 `tmp/N4-07-child-cancelled-owner-review-v1-20261116.json`，包含 622 个入口与传递依赖源码散列。

| 专业流程 | 实际失联位置 | 取消与恢复要求 | 后续实际功能 |
|---|---|---|---|
| Windows Pandoc 报告 | child 完整准备已保存，child_running 尚未保存 | 原 worker 硬退出后，通过实际 API 保存取消；原 child 仍 preparing，identity/stop 仍缺失，完整 Windows Job 范围已停止 | 旧 HTML/DOCX 字节保留；重新生成新 HTML/DOCX 并核验内容 |
| WSL MPI 求解 | UID65534 child 原入组已保存，child_prepared/child_running 均未保存 | 原 worker 硬退出后取消；原字段与事件缺失不补造，核验原 cgroup 撤销和原 gate 退出 | 旧 0.5/U 字节保留；新双进程、400 单元求解与发布完成，串并行结果误差检查通过 |

测试先关闭空闲 peer，避免取消请求保存前任务已经被另一个 worker 恢复。故障调用原生产函数并真实提交后才执行 `os._exit(17)`。取消通过实际 API 提交，新 worker 再读取原记录收尾；未制造进程退出、SQL 结果或模型答复。

两条路径都要求 job/attempt 最终为 cancelled、reason 为 cancel_requested，检查点中的取消时间与原请求精确相等。原 input、publication、process_identity 和 child 日志保持原字节，原任务只有一个 attempt，无自动重放、无伪造 stop。专业处理之后还有真实 Linux 任务生成证明文件，最终用官方 DeepSeek 的文件工具回合读取。

Windows 和 Linux 分别只读校验同一实际恢复检查点；改变原取消时间或删除该时间必须被拒绝。该类检查属于纯凭据检查，和真实模型调用分开统计。

## 仍需完成

本次证明上述两类实际专业流程的取消恢复，不代替更早的未完整准备窗口、其他外部 owner 或完整部署故障矩阵。N4-05、N7 和 295 项全功能交付仍按原计划继续；不新增权限层或退出条件。

## 最终结果与归档

2026-11-16 N4-07 专业 child 取消恢复验收补齐：沿用上一轮生产实现，真实 Windows Pandoc child 完整准备后失联及 WSL MPI child 入组后、child_prepared/running 前失联，均经实际API取消和新worker恢复。原取消时间、缺失identity/stop/prepared和原输入发布记录不补造；旧HTML/DOCX及0.5/U保留，新报告与双进程400单元求解发布通过，后续Linux任务和真实模型文件读取通过。共13个实际任务、20个原生子执行、2次官方DeepSeek deepseek-flash HTTP200、模拟0；74项纯凭据检查另列，57个记录原进程身份已消失。专属Redis/S3停止、临时PG删除和访问规则恢复。见 professional-child-cancellation-recovery.md。主线48/53、剩5；295项交付状态保持。下一实际代码缺口：native.recover 跳过原 RETIRED child，但 scope 证明仍要求全部 native child 的独立退休记录；需复用原完整停止证据衔接已完成子步骤与后续失联。更早准备、外部owner及N4-05/N7原范围继续。

实际证据 `tmp/remote-worker-iilxqgmn`，数据库清理 `tmp/remote-worker-postgres-9b4f05778c034f5382f52ac941a395a6`。源码归档 `platform/backups/mainline-N4-07-child-cancelled-owner-20261116.zip`，校验记录 `tmp/mainline-N4-07-child-cancelled-owner-evidence/validation.json`。本轮生产源码与上一轮归档一致；变更为实际验收入口及其参数化辅助代码。
