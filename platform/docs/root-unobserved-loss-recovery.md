# 首条启动观察未保存时的根执行恢复

2026-11-16，继续 N4-07 原失联恢复条件。复用既有部分范围视图、独立 guardian、完整内核撤销及 v4 检查点。

## 原缺口与修复

实际宿主已通过原子 Job 归属创建，但 worker 在 `attempt.launch_observed` 第一条观察提交前退出时，原 SQL 没有宿主出生身份，旧根准备读取仍拒绝空观察链。范围的启动意图与独立 guardian 已保存，WSL 内核分配也已保存，任务却不能完成恢复收尾。

现允许已绑定原意图的空观察链返回明确部分范围视图，host/native 均为 null。独立 guardian 仍需证明原 owner 和 guardian 退出、整个 Windows Job 已关闭，且绑定原空观察历史摘要。恢复证明中的 `host_identity` 和 `host_exit` 必须存在并明确为 null；不能删除未知事实字段，也不能伪造某个原宿主的 EXIT。WSL 继续要求完整原分配范围已撤销，未知原生出生/退出同样保留为空，并拒绝后续加入该范围。

这不会把没有独立 guardian 或没有对应原内核分配的请求自动当作停止；其余原条件仍按台账推进。原 running、prepared、admitted、stop 和观察事件不补写，原公开文件不覆盖。

## 实际验收

显式入口 `live_llm_root_unobserved_loss_acceptance.py` 使用真实 PostgreSQL、S3、Redis、Windows API 与 Windows/Linux worker：

- Windows 实际创建后、第一条观察提交前退出。
- WSL 同窗口退出，原内核范围实际撤销并拒绝迟到加入。
- WSL 同窗口退出后经实际 API 取消，由新 worker 恢复为 cancelled。
- 兼容上一轮 WSL created 已保存的路径。

钩子接收实际 `OwnedProcess` 创建身份，在原观察 SQL 函数执行前退出 worker（17）。创建身份只保存在隔离测试证据中，供独立测试进程核验真实存活和退出；生产恢复没有接收这份未提交身份。SQL 观察数量始终为零，原命令未执行。各场景随后实际执行新命令并发布随机文件，普通 WSL 为 UID65534，最后由真实 DeepSeek 文件工具读取。

Windows/Linux 分别核验凭据，删除未知身份字段、伪造观察或退出、不完整 Job 成员闭合、缺少内核撤销等纯协议检查单列，不计作模型调用。无模拟 LLM 回归。

## 最终结果与归档

2026-11-16 N4-07 首条启动观察缺失恢复已修复：真实Windows/WSL进程创建后、原SQL观察提交前退出，复用原意图/guardian/内核分配关闭完整范围；原host/native身份及逐进程exit仍为null，原观察/登记/stop不补写。Windows、WSL、WSL恢复前取消及原created兼容四场景通过；旧产物保留，新UID65534命令发布成功，三次实际迟到加入原范围均拒绝。最终12个实际任务、2次官方DeepSeek deepseek-flash HTTP200、模拟0；152项纯凭据检查另列，35个由独立测试记录的原进程身份已消失。专属Redis/S3停止、临时PG删除及访问规则恢复。见 root-unobserved-loss-recovery.md。主线48/53、剩5，295交付状态保持；原分配/意图/guardian更早未确认、专业child部分准备、外部owner及N4-05/N7原范围继续。

实际证据 `tmp/remote-worker-zzobvc4j`；数据库清理 `tmp/remote-worker-postgres-082fe1fcdd5c472bab27950d77f7dd83`。源码归档 `platform/backups/mainline-N4-07-root-unobserved-loss-20261116.zip`，校验记录 `tmp/mainline-N4-07-root-unobserved-loss-evidence/validation.json`。执行源码与本轮真实审查散列一致。
