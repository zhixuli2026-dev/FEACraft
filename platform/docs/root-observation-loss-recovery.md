# 根启动观察已保存、确认尚未提交时的失联恢复

2026-11-16。对应 N4-07 原有“失联可恢复”条件，沿用独立守护、原内核范围和 v4 排空检查点。主线及全功能目标不变。

## 修复的问题

Windows gate 的 `host_ready`，或 WSL gate 的 `native_ready` 已写入原启动观察链，但 worker 在 `launch_prepared` / `native_scope_admitted` 提交前退出时，旧恢复要求这些后续确认存在。实际身份已经可知，任务仍不能排空，阻止后续发布。

`job_root_preparation` 现可从完整原观察链读取本地核验用的 gate 身份视图。原 `prepared`、`admission`、running 身份和 stop 的缺失事实保留，既不补写原事件，也不重新执行旧命令。仍拒绝只到 `created` 或 WSL `host_ready` 的不完整链。

对于缺少入组确认的 WSL 根，`job_native_scopes` 核对原分配的 nonce、发行版、boot、UID 和完整原观察，撤销整个原 inode 绑定的内核范围，并独立检查原 gate 退出。恢复结果中 admission 明确为 null；不会生成“曾完成入组确认”的回执。Windows owner、独立 guardian、完整 Job 范围关闭要求保持。原入组确认存在时继续核对原记录。

## 实际验收入口

`platform/server/live_llm_root_observation_loss_acceptance.py` 复用真实 PostgreSQL、S3、Redis、Windows API 和 Windows/Linux worker，依次执行：

1. Windows 原 `host_ready` 提交后硬退出，准备及 running 登记缺失。
2. WSL 原 `native_ready` 提交后硬退出，入组确认、准备及 running 登记缺失。
3. 同一 WSL 窗口硬退出后，经实际 API 取消，再由新 worker 恢复为 cancelled。
4. 原 WSL 入组已确认、准备确认缺失的兼容路径。

故障钩子先等待真实原函数提交，再用实际 `os._exit(17)` 退出对应 worker；不是替换执行结果。原 payload 的未执行标记、原公开文件、原输入与发布记录、缺失确认数量和停止事实均核验。每个场景随后执行新命令并发布随机文件证明，最终由真实 DeepSeek 文件工具读取。普通 WSL 实测 UID65534。

Windows 和 Linux 分别校验恢复凭据；删除观察、打乱顺序、改变身份/UID、伪造入组、缺少内核撤销等拒绝检查单列为纯协议检查，不计作真实模型调用。没有运行模拟 LLM 回归集合。

## 最终结果与归档

2026-11-16 N4-07 原启动观察完整、确认缺失的恢复已修复：读取原 Windows host_ready / WSL native_ready 身份视图，保留缺失 prepared/admitted/running/stop；核对原分配并实际撤销完整范围，不伪造入组确认。Windows、WSL 失联、WSL 取消及已有入组兼容四场景通过；原 payload 未执行、旧文件保留，新命令发布成功，普通 WSL UID65534。共12个实际任务、2次官方DeepSeek deepseek-flash HTTP200、模拟0；134项纯凭据检查另列，38个记录原进程身份消失。专属Redis/S3停止、临时PG删除及访问规则恢复。见 root-observation-loss-recovery.md。主线48/53、剩5，295项交付状态保持；更早不完整观察/分配、其余外部owner及N4-05/N7原范围继续。

实际证据 `tmp/remote-worker-7penlt_p`；数据库清理 `tmp/remote-worker-postgres-d8d766b0aafd42959faa7733f92042c3`。源码归档 `platform/backups/mainline-N4-07-root-observation-loss-20261116.zip`，校验记录 `tmp/mainline-N4-07-root-observation-loss-evidence/validation.json`。执行源码与本轮真实审查散列一致。

后续 created / WSL host_ready 部分观察窗口也已完成实际恢复，见 [根创建失联恢复](root-creation-loss-recovery.md)。上文拒绝这些窗口的描述是当轮边界，后续未伪造原生身份。
