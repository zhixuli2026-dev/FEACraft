# 运行中取消与 worker 失联的完整收尾

2026-11-16，N4-07 增量。主线 53 项和全功能 295 项范围保持。

## 修复的问题

用户取消会先保存 job=cancelled、attempt=cancelling，实际执行停止后再完成 attempt。旧 Windows/WSL 范围恢复与 Linux namespace 恢复只接受 interrupted/recovery_required；如果原 worker 在取消收尾前退出，即使独立停止证明齐全，恢复仍会拒绝该任务，继续阻止算例写入。

现在取消收尾复用已有完整范围证明：先确认原 worker 出生身份退出，核验原 Windows Job、WSL cgroup 或 Linux namespace 的完整停止，再在算例锁内重核原状态和取消时间，完成 cancelling→cancelled。原请求时间写入检查点并进行精确校验；不会将用户取消改记为 interrupted，不重放旧命令，也不补造原 worker 的 stop 回调。

原 root 身份、child 日志、输入和发布绑定保持不变。失败发布预留按 cancelled 收尾，旧成功产物保持原字节；完成后可正常提交新任务。普通 interrupted 恢复和上一阶段启动取消的凭据格式继续可读。未登记完整的专业 child 可进入同一准备记录核验路径，但其全部取消组合仍需原范围验收，不能用本次根任务结果代替。

## 实际验证方法

显式入口 `platform/server/live_llm_cancelled_owner_loss_acceptance.py`。审查登记 `tmp/N4-07-cancelled-owner-loss-review-v1-20261116.json` 包含 621 个入口和传递依赖源码散列。使用独立 PostgreSQL、S3、Redis 和真实 Windows/Linux worker。

Linux 场景启动实际 namespace 内的命令及忽略 SIGTERM、脱离原会话的后台写入进程，核验完整原 namespace 成员。分别验证普通失联和原 worker 退出后收到取消，再由新 worker 恢复。

Windows/WSL 场景在原生产函数已经释放实际 payload 后暂停原调用返回，业务代码持续写入心跳文件。通过实际 API 保存取消，核对原 worker/进程仍存活，再让 worker `os._exit(17)`。暂停采用等待收尾机制，保证取消不提前触发原 stop 回调；没有替换进程结果、数据库提交或模型输出。恢复须保留原取消时间、停止实际心跳、保留旧公开文件并完成新命令发布。

另复核原 WSL 入组后、`launch_prepared` 前失联并取消的兼容路径。每次新文件内容均由实际命令生成随机值，最终通过官方 DeepSeek 的真实文件工具回合读取；纯凭据篡改检查单独计数。

## 未完成范围

这次对应运行中取消和失联的交错，不替代更早的不完整启动观察、全部专业子执行取消、其他外部 owner 和 N4-05 资源预算。N7 的完整故障矩阵、部署和全功能交付评审继续。没有增加权限层或追加阶段退出条件。

## 最终结果与归档

2026-11-16 N4-07 运行中取消与失联收尾已修复：旧恢复仅接受 interrupted，导致 job=cancelled/attempt=cancelling 无法完整排空。现复用原完整范围证明并锁内复核取消时间，完成 cancelled 收尾，保留原身份、输入、发布与缺失 stop。真实 Linux worker 退出后取消、Windows/WSL 取消已提交且进程仍活时退出均通过；后台心跳停止，旧产物保留，新命令可发布。同轮普通Linux中断、WSL入组后启动取消兼容通过。共14个实际任务、2次官方DeepSeek deepseek-flash HTTP200、模拟0；100项纯凭据检查另列，49个记录原进程身份已消失。专属Redis/S3停止、临时PG删除及访问规则恢复。见 cancelled-owner-recovery.md。主线48/53、剩5；295项交付状态保持，更早不完整准备、专业child其余取消、外部owner及N4-05/N7范围继续。

实际证据 `tmp/remote-worker-f6a44bl2`；数据库及访问规则清理 `tmp/remote-worker-postgres-f489b3ae4b5749baac204cc3d1bf0320`。源码归档 `platform/backups/mainline-N4-07-cancelled-owner-loss-20261116.zip`，校验记录 `tmp/mainline-N4-07-cancelled-owner-loss-evidence/validation.json`。五个场景均保留原 attempt，不重放，随后实际新任务成功。
