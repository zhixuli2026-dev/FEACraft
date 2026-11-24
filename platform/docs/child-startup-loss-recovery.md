# 报告子执行准备期间的 worker 失联恢复

2026-11-16。对应 N4-07 的“失联可恢复”，沿用现有准备事件、Windows Job 守护凭据、owner 退出核验和 v4 排空记录，没有增加权限层或阶段退出条件。53 项主线、295 项全功能交付目标保持有效。

## 问题与修复

Pandoc 子进程已创建，完整 `child_launch_prepared` 已提交，但 worker 在 `child_running` 提交前硬退出。原子执行日志仍为 preparing，identity/stop 都为空。旧逻辑只用 running 日志查找 Windows 范围，忽略已经保存的完整准备链，因此无法恢复。

- `child_launch_registration` 从原始 reserved、intent、observed、prepared 事件读取完整 Windows 子进程身份。拒绝缺失、乱序、重绑定、已有 running/stopped 或 aborted 的记录；当前新增路径只处理完整准备的 Windows 子执行。
- `job_scope_guardians` 使用这份原始准备身份核对既有守护请求、Job 范围和实际退出回执，写回前重新核对原记录。
- `job_registration_recovery` 复用原 owner 退出处理，将原 attempt 标为 interrupted；`job_scope_recovery` 在完整范围凭据通过后记录 `vibeflow.job-child-startup-recovery.v1`。原 child.identity、child.stop、缺失的根停止回调及输入清单保持不变；不重放原命令，不把独立恢复伪装成原 worker 的正常停止。
- 失败发布预约使用原有 finalize 处理，已发布的旧报告不被覆盖；显式提交的新报告可正常生成和发布。

## 实际联调发现的跨主机问题

第一轮实际 worker 硬退出、恢复、旧报告保留和新的 Windows HTML/DOCX 生成均通过，但随后的 Linux 证明任务在发布校验时失败。这一轮整体不算通过，且尚未调用真实模型。

原因是恢复校验会重新验证登记的报告处理器，而旧报告契约携带 Windows worker/Pandoc 路径；Linux 将它当作本机路径，报 `Unexpected report worker`。失败证据保留于 `tmp/remote-worker-z5j4toeg/`；只读重核原实际封存契约的结果在 `tmp/N4-07-child-loss-prior-contract.json`。该检查是纯元数据诊断，不是模型测试。

修复复用 `handler_environment` 已有机制，为新提交且配置了主机映射的 `report.build` 声明原 worker 文件、散列和解释器角色；验证时解析到本机相同版本的登记程序。Pandoc 参数继续保留原封存路径与散列，按该路径原有的 Windows/POSIX 语法检查绝对路径。实际报告仍路由到已配置的 Windows Pandoc 主机。本轮不宣称 Linux 已安装或可执行 Windows Pandoc，也不改写历史请求。

## 验收入口与证据分类

显式入口：`platform/server/live_llm_child_startup_loss_acceptance.py`。所有导入辅助代码沿用逐文件散列审查，新增/变更源码另行审查。未启动历史模拟模型回归。

1. **实际基础设施**：独立 PostgreSQL、S3、Redis，Windows API/worker 和非特权 Linux worker。先生成真实报告，再让 Windows worker 在原 prepared 提交后实际 `os._exit(17)`，另一个 worker 自动恢复。核对原输入、子执行记录、单一 attempt、无新增 running 登记、旧 HTML/DOCX 字节不变。
2. **实际功能**：重新提交报告，下载 HTML 和 DOCX，并核对新正文。实际 Linux 命令将下载产物散列写入证明文件；之后继续原基线的真实用户取消、关闭中断及重启后新任务。
3. **纯凭据检查**：Windows 和 Linux 对同一实际数据库内的恢复凭据进行校验，每端拒绝 12 种篡改，包括缺失准备链、改变 claim、准备进程身份、乱序、伪造 stop、活跃 owner 和错误 schema。检查不修改数据库，不计为真实模型调用。
4. **真实模型**：使用用户已授权的官方 DeepSeek 服务，通过平台 Agent 实际选择文件工具读取产物证明。是否通过、实际模型和 HTTP 调用数以最终 live-result/upstream 证据为准；失败不回退模拟。

最终重跑证据：`tmp/remote-worker-u3hl_27p/`。全部 8 个实际任务通过预期结果验收（包含故障任务 interrupted、用户取消及关闭中断），17 个原生子执行中包括故障窗口的准备身份；`child-startup-exits.json` 独立确认 36 个原进程身份均已退出。Windows/Linux 各 12 项凭据篡改拒绝，共 24 项纯检查。官方 DeepSeek `deepseek-flash` 实际 2 次 HTTP 200，模拟模型调用 0。

最终临时数据库清理和访问规则恢复证据：`tmp/remote-worker-postgres-560a0ba9c960483db5c14d07c83b25cb/`。最终及前置失败轮的 PostgreSQL 均已删除，访问规则均恢复，专属 Redis/S3 已停止。失败轮还核验了所保留证据中的 27 个原进程身份均不再存在；此数不宣称覆盖未完成证明任务的全部进程记录。

审查记录：`tmp/N4-07-child-startup-loss-review-v1-20261116.json`（失败轮）、`tmp/N4-07-child-startup-loss-review-v2-20261116.json`（最终通过轮）。最终生产和验收源码与 v2 审查逐文件一致，未在验收后修改实现。源码及证据归档核验：`tmp/mainline-N4-07-child-startup-loss-evidence/validation.json`。

## 剩余范围

本轮不关闭整个 N4-07，也不把根退出当作完整 Linux 子树停止。未完成准备链、WSL 专业 child 缺失 running 登记、其他取消组合、外部 owner 和原计划中的剩余恢复条件继续核对。N4-05 资源预算、N7-01 故障矩阵、N7-05 发布/部署、N7-06 全功能审计仍保持原任务范围。
