# 收尾记录写入失败后的恢复

2026-11-16，N4-07 阶段修复；整项仍未关闭，主线 48/53。

## 修复的问题

命令已正常停止、不可变发布产物已完整写入，但数据库拒绝写入最终排空记录时，原 worker 退出后应能恢复收尾。此前恢复代码无条件验证原运行目录；更换 Linux worker 后，本地缓存为空，即使 S3 产物完整也无法完成恢复。

现在复用原有恢复流程：产物已经完整验证时，重新核验不可变 bundle 并完成 readiness 记录，不再依赖旧运行目录；尚未复制完整时，仍要求原始输出目录及文件散列一致。已验证的产物随后消失时直接失败，不凭空重新创建产物。原始停止凭据、输入、发布身份、worker 退出观察和持久化事件的要求保持不变。

改动位于 `publication_preparation_recovery.py`、`publication_journal.py`、`publication_objects.py`。没有新增权限层、隔离层或恢复协议。

## 实际验收

入口 `platform/server/live_llm_remote_drain_failure_acceptance.py` 使用实际 PostgreSQL、S3、Redis、Windows API 和非特权 Linux worker：

1. 测试数据库中的实际触发器拒绝原始排空 checkpoint 更新，返回 PostgreSQL `P0001`。命令确实停止，bundle preparation 已持久化，但 checkpoint 为空。原 worker 仍活着时不抢先恢复。
2. 原 worker 真正退出后，实际改坏隔离 S3 bucket 中的一个产物。新 worker 使用独立空缓存；恢复被拒绝，任务不误报成功，不补造排空记录。
3. 还原原始对象字节，周期恢复完成原 attempt。旧运行目录在恢复前后都不存在；原输入散列、worker/进程身份和发布身份保持不变。
4. 写入独立的 v3 恢复排空凭据，保留原停止证明；排空与最终恢复事件各一条，`replayed=false`、`native_execution_started=false`。未重跑业务命令。
5. 真实 DeepSeek `deepseek-flash` 通过工具读回恢复产物中的随机证明，完成两次 HTTP 200 请求及结果持久化。

证据：`tmp/remote-worker-eccxkfa_/drain-failure.json`、`remote-result.json`、`live-result.json`、`job-exits.json`。共 5 个实际远端任务，包括恢复、普通执行、取消、停机和重启。数据库清理及临时访问规则恢复记录位于 `tmp/remote-worker-postgres-404dfb57c7f147b29d00cace367afdef/`。Redis/S3 已停止，5 个原生任务根进程经独立查询确认退出。

源审查 `tmp/N4-07-remote-drain-review-v1-20261116.json` 覆盖 600 个来源散列。SQL 触发器、S3 损坏和恢复属于实际基础设施故障测试；模型回复、工具选择和流式输出均来自真实服务，模拟模型调用为 0。

默认 Windows/WSL、FS/SQLite 流程兼容性结果在阶段归档验证文件中单独记录，不将远端 S3 结果当作本地文件系统恢复路径的完整故障验收。

## 剩余范围

此修复覆盖“原生停止证明已保存、不可变产物完整、最终排空记录写入失败”。不代表缺少原始停止证明的执行中失联已经验收。N4-07 仍需按原计划完成执行中失联收尾/接管、专业 handler 环境映射和其余外部 owner 策略；不增加新的阶段退出条件。主线剩 5 项，295 项全功能交付状态不因本轮局部验收而关闭。
