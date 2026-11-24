# Windows/WSL 根任务启动失联恢复

2026-11-16，N4-07 增量。原主线 53 项、全功能 295 项范围保持，不能以本次四个窗口代替整项验收。

## 修复的问题

原 worker 可以先保存完整启动准备或 WSL 入组记录，再登记 running。若在这两步之间退出，原 `process_identity_json` 仍为 `{}`。旧 Windows 守护与 WSL 内核范围恢复都要求这个字段已有身份，因此即使原准备记录和独立守护凭据齐全，任务仍无法完成恢复。

新增 `job_root_preparation` 只读取原意图、按顺序保存的观察和准备记录；WSL 缺少 `launch_prepared` 时可使用同一原始入组记录。所有记录仍须匹配原 attempt、输入、worker、启动随机标识及完整观察链。它供现有 guardian、内核范围退休和 v4 检查点共用，不增加权限层，也不重启旧命令。

恢复后的 `vibeflow.job-root-startup-recovery.v1` 保留原根身份字段、子执行日志和停止回执缺失；不补写 running 或 prepared 事件。原 worker 的确切出生身份退出、Windows Job 范围关闭和 WSL 原 cgroup 撤销仍分别核验。只在持有算例锁并复核原快照/准备记录后，完成原 attempt 的 interrupted 收尾；已收到取消则保留取消时间，完成 cancelled 收尾。原成功产物不变，失败发布预留收尾后允许新任务正常执行。

## 实际验收

入口：`platform/server/live_llm_root_startup_loss_acceptance.py`。审查：`tmp/N4-07-root-startup-loss-review-v1-20261116.json`，包含 620 个入口与传递依赖源码散列。测试调用原生产函数，待真实提交完成后以 `os._exit(17)` 退出原 Windows Redis worker；没有用固定模型答复、假进程或假数据库结果替代。

| 实际中断位置 | 原记录与结果 | 恢复后实际操作 |
|---|---|---|
| Windows `launch_prepared` 后、running 前 | 原根身份仍为空，原 payload 未执行，任务 interrupted | 新 Windows 命令生成并发布文件 |
| WSL `launch_prepared` 后、running 前 | 原 UID65534 gate 与原内核范围结束，任务 interrupted | 新 UID65534 命令生成并发布文件 |
| WSL `native_scope_admitted` 后、`launch_prepared` 前 | 原 prepared 事件仍为零，原 payload 未执行，任务 interrupted | 新 UID65534 命令生成并发布文件 |
| 上述 WSL 入组窗口，worker 退出后收到取消 | 原取消时间保留，任务 cancelled | 新 UID65534 命令生成并发布文件 |

四个场景均维持单个原 attempt，原输入/发布记录/缺失停止字段不变，旧公开文件字节不变。每个场景在 Windows 和 Linux 上只读校验同一真实检查点；共 128 项改动凭据检查拒绝通过，单独计入纯协议检查，不计作模型调用。

主验收证据：`tmp/remote-worker-ufxm4da_/`。包含四个故障任务、四个恢复后新任务、四个原远程基线任务，共 12 个实际任务。DeepSeek 官方服务 `deepseek-flash` 实际调用 2 次 HTTP 200，通过文件工具读取新任务随机生成的内容；模型提示不提供该随机内容，模拟模型调用为 0。原 Linux 基线同时验证用户取消、worker 关闭为 interrupted、重启接受新任务。

`root-startup-exits.json` 独立复核 38 个已记录原进程出生身份均已消失。专属 Redis/S3 停止；`tmp/remote-worker-postgres-91f146bda1394214ab6bcd7f3f532dc2/` 记录临时数据库删除和访问规则恢复。开发库仅复用已有模型配置，未修改业务数据。

## 边界与后续

这次补齐的是已有完整启动观察和准备/入组证据的根执行窗口。更早的不完整观察、其他原定取消组合与外部 owner 条件仍按 N4-07 原任务核对；未知范围仍不能凭单个 PID 退出认定完整停止。N4-05 的完整资源预算、N7 的故障矩阵/部署说明/全功能评审仍保留，不能将此增量写成全部 295 项完成。

## 共用恢复路径兼容与归档

同版源码另执行 `live_llm_wsl_child_startup_loss_acceptance.py`，审查 `tmp/N4-07-root-startup-compatibility-review-v1-20261116.json`。实际串行/MPI参考、子执行准备后原worker退出、完整旧范围停止、新双进程400单元MPI结果及Linux证明文件发布均通过。证据 `tmp/remote-worker-24l5s0_z`，9个实际任务、3个原生子执行、2次真实DeepSeek调用；36项双主机纯凭据检查另列。独立退出核验记录 27 个原出生身份消失，临时PG清理记录 `tmp/remote-worker-postgres-66899f96e07b450a93c1eea1852ad1d0`。

两轮合计21个实际任务、4次真实模型调用、164项纯凭据检查。源码归档 `platform/backups/mainline-N4-07-root-startup-loss-20261116.zip`，核验 `tmp/mainline-N4-07-root-startup-loss-evidence/validation.json`；未将本次增量计为整个 N4-07 或 295 项交付完成。
