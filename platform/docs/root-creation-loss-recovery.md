# 根宿主创建及原生身份未知窗口的失联恢复

2026-11-16。对应 N4-07 的失联恢复；复用现有 Windows Job guardian、原分配的内核范围和 v4 排空检查点。

## 行为与边界

原 `created` 观察已持久化而宿主握手未完成，或 WSL 只有 `host_ready` 而原生身份尚未知时，旧逻辑必须等待完整身份，故 owner 退出后无法继续收尾。

现在 `job_root_preparation` 对这些原观察返回明确的 `root-startup-scope-view.v1` 范围视图，包含原宿主出生身份和原启动 nonce 对应的 Job。它不是完成握手的 process record，也不会写入原 prepared/running 记录。独立 guardian 仍须核验实际 owner、原宿主与 guardian 退出，以及整个 Windows Job 关闭。

WSL 另要求原已分配的、inode 绑定的内核范围实际撤销。原 native gate 未观察到时，恢复回执的 `native_gate_exit` 保持 null，检查器拒绝把它替换成虚构 EXIT。原 gate 实现必须先成功加入该范围，再发出原生握手和接受执行许可；撤销后内核拒绝迟到加入，所以未知原生身份不能成为绕过范围撤销的执行许可。不会宣称未观察的进程出生或退出状态。实际新任务仍使用 UID65534。

独立 guardian 的观察同时保存本次核验的准确宿主出生身份。部分范围视图必须与它一致，防止只含一条 created 观察时替换宿主 PID。已有完整身份的历史回执仍兼容；新回执一旦带有身份，必须核对一致。

独立观察还绑定原 intent、prepared、完整 observations 和 admission 的摘要，防止删掉最后一条观察而改称较早阶段。取消时间由原检查点单独核对，不纳入这份不可变启动历史摘要，避免正常晚到取消改变原范围观察的含义。

## 验收与首轮失败

显式入口 `live_llm_root_creation_loss_acceptance.py` 在真实 PostgreSQL、S3、Redis、Windows API、Windows/Linux worker 上执行 Windows created、WSL created、WSL host_ready、WSL host_ready 后取消，以及已完成 native_ready 的兼容场景。故障在原生产观察实际提交后退出对应 worker（17），不替换生产返回值。

每个场景确认原命令未执行、原公开文件和缺失登记不变，随后实际新任务发布随机文件；最后使用真实 DeepSeek 文件工具读取。三个原生身份未知场景还实际观察原分配从 empty 变为 absent，并通过真实 Linux 内核加入调用验证原范围已拒绝迟到加入。

首轮 `tmp/remote-worker-_gp4d8_j` 中实际 Windows created 恢复已完成，但修改凭据副本中宿主 PID 被错误接受，整轮按失败保留，未进行真实模型调用。增加独立观察的宿主身份绑定后重新审查并整轮重跑。首轮两个实际任务对应的 7 个记录出生身份已核验消失，临时 PG 已删除；这是失败清理证据，不是通过验收。

第二轮 `tmp/remote-worker-vpacbsch` 的 Windows/WSL created 已通过，WSL host_ready 的实际恢复与迟到加入拒绝完成，但截短观察链的凭据仍被接受，整轮也按失败保留。补齐上述不可变历史摘要后再次审查重跑。第二轮真实模型 0，6 个实际任务涉及的 18 个记录出生身份已核验消失，临时 PG 已删除；不计作最终通过。

## 最终结果与归档

2026-11-16 N4-07 更早根创建恢复已修复：Windows/WSL created 和 WSL host_ready 复用原 guardian/分配范围，明确部分范围视图，未知 native gate 出生/退出仍为null；实际撤销完整内核范围并拒绝三次迟到加入。独立 guardian 观察绑定准确宿主出生身份。五场景含取消和原native_ready兼容均通过，原payload未执行、旧文件保留，新UID65534命令发布成功。最终14个实际任务、2次官方DeepSeek deepseek-flash HTTP200、模拟0；188项纯凭据检查另列，42个记录原进程身份消失。前两轮修改宿主PID及截短原观察链的凭据拒绝检查失败，分别补齐独立宿主身份和完整不可变启动历史摘要绑定后整轮重跑；两失败轮模型0、共8个实际任务及7+18个记录身份仅作为失败/清理证据。三轮专属Redis/S3停止、临时PG删除及访问规则恢复。见 root-creation-loss-recovery.md。主线48/53、剩5，295交付状态保持；更早无观察/分配确认、专业child部分准备及外部owner等原范围继续。

实际证据 `tmp/remote-worker-_h_54rui`；数据库清理 `tmp/remote-worker-postgres-aebcddd52b9b4ad1b1f0a6eb8c98aa34`。源码归档 `platform/backups/mainline-N4-07-root-creation-loss-20261116.zip`，校验记录 `tmp/mainline-N4-07-root-creation-loss-evidence/validation.json`。执行源码与本轮真实审查散列一致。
