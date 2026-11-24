# N4-07 原始退出评审

2026-11-17。按 [原计划](next-stage-mainline-plan.md) 的三项退出条件和原任务名称评审，不以新增故障细分数量定义完成。

| 原条件 | 实现与实际依据 | 结论 |
| --- | --- | --- |
| 优雅关闭、紧急关闭、看门狗及 Redis worker | 停止领取、宽限期、取消 delivery 后等待原收尾；实际 Redis/Windows/WSL worker 的关闭、运行中硬退出和重新启动 | 已有实际验收；见 worker-shutdown-drain.md、worker-owner-recovery.md、linux-worker-loss-recovery.md |
| 关闭与用户取消区分 | delivery 关闭保存 interrupted，用户取消保存 cancelled；原取消意图在恢复中保留。终端正常 API 退出另保存 shutdown | 已有实际验收；见 cancelled-owner-recovery.md、terminal-normal-shutdown.md |
| 完整收尾后确认任务 | 真实启动交接和 PDF finalizing 窗口内，Redis in-progress 键保留；停止凭据和最终 SQL 提交完成后才结束 worker；最终记录拒写可独立恢复 | 已有实际验收；见 N4-07-original-criteria-audit.md、worker-drain-recovery.md |
| 失联可恢复 | 根执行、专业 child 登记完整/不完整、观察缺失、分配和 guardian 交接，以及已完成子步骤衔接失联均已有命名实际场景；旧发布保留，新请求可发布新产物 | child 最后九组已补齐，47 个实际任务、6 次真实 DeepSeek 调用；见 child-handoff-loss-recovery.md |
| API 相关执行收尾核对 | 原 API 本地任务、Viz、Codex 已有所属关闭入口和既有会话验收；本次核查发现的终端遗漏已修复，实际 FS/S3 + cmd/WSL 正常退出、持久收尾、重启读回通过 | 已知具体遗漏已关闭；见 N4-07-owner-closure-audit.md、terminal-normal-shutdown.md |

## 证据边界

首轮 child 交接因隔离 S3 容量不足失败，生产源码不变，保留六组通过后在新环境补齐三组和原基线；失败未被覆盖。模型真实调用与实际任务、300 项纯凭据检查分别统计。首轮终端 UID 0 失败和最早正常关机缺失记录也保留，最终 FS/S3 两组重新执行通过。没有运行历史模拟 LLM 回归。

活动或未知写者仍阻止接管；缺少完整停止依据的旧历史记录不伪造、不自动升级。当前支持的处理器和执行主机选择不等于任意专业工具在任意系统均可用。已有同机 Windows/WSL 实际 host ID 证据不冒充物理多主机。

这些边界原本分别属于 N4-05 的统一资源/历史维护、N7-01 的完整跨环境与故障矩阵、N7-05 的依赖和部署交付，继续保留于对应台账；没有因关闭 N4-07 删除它们。295 项全功能逐项交付仍需 N7-06 审计，不随本项自动完成。

本评审结论：N4-07 按原退出条件完成。主线应更新为 49/53，剩 N4-05、N7-01、N7-05、N7-06 四项；最终证据及源码归档通过后由台账记录完成时间。
