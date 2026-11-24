# FF-H18 Windows 作业 CPU 与内存额度

Windows worker 在作业首次写入尝试时冻结 CPU 与内存配置。可信启动器创建 Windows Job Object 后、任何成员进入前，设置整棵进程树的累计用户态 CPU 上限和提交内存额度。运行中每 100 ms 读取所在 Job Object 的实际计量。到达额度时走原作业停止、范围核验和 `resource_limit` 分类，保留墙钟和日志上限。CPU 采纳内核结束信号，也用采样缩短内核轮询的延迟。 [Microsoft Job Object 限制](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_limit_information)

在 Windows 启动配置中设置 `VIBEFLOW_EXECUTION_WINDOWS_CPU_LIMIT_SECONDS` 和 `VIBEFLOW_EXECUTION_WINDOWS_MEMORY_LIMIT_MB`。默认值均为 `0`，在当前部署按需启用。冻结后的原额度随重试保留，后续新作业采用新设置。正式作业详情中的 `attempt.windows_compute_usage` 保存额度、累计用户态和内核态 CPU 时间、原生 Job Object 内存峰值、成员数及超限类型。CPU 数值使用 100 ns 单位。Windows 的原生峰值在实测中包含被拒绝的内存请求，判断实际拒绝同时读取了 `VirtualAlloc` 返回码。 [Microsoft 扩展限额与计量](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_extended_limit_information)

## 2026-11-22 实测

| 验收 | SQLite | PostgreSQL |
| --- | --- | --- |
| 1 秒累计用户态 CPU 限额 | 约 1.09 秒，持久停止证明完整 | 约 1.08 秒，持久停止证明完整 |
| 128 MiB 内存限额 | 实际 256 MiB 内存申请返回 1455，停止且失败 | 实际 256 MiB 内存申请返回 1455，停止且失败 |
| 小作业、API 重启和策略更新 | 小作业成功，三组用量重启可读，原尝试重试维持旧额度 | 小作业成功，三组用量重启可读，原尝试重试维持旧额度 |

首轮观察到单独依赖内核 CPU 检查时，1 秒限额在约 6.72 秒累计用户态时间触发。另外两轮复验发现新的停止原因需纳入持久停止回执，修正后双库通过。验收中三个隔离 PostgreSQL 库均已删除，所有隔离服务监听关闭。Windows 限额专项用了实际作业和进程，模型调用 0。新增默认配置回归运行五个真实作业，通过 `deepseek-flash` 的工具读回文件，2 次 HTTP 200，模拟 0。 [复验、原失败和清理证据](../../tmp/FF-H18-windows-compute-20261122/validation.json)

## 注册工具与收尾进程

注册报告作业的主进程、独立工具和收尾进程在创建时加入同一个外层 Job Object。各进程范围同时保留单独的停止回执。CPU 与内存计量由外层范围汇总，最后一批工具完成后核实活动进程为零，再写作业用量。 [Windows 嵌套 Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/nested-jobs) · [进程创建实现](../server/app/services/windows_process_create.py)

SQLite 和 PostgreSQL 在 512 MiB 限额下均生成并核对实际 HTML 和 DOCX。两组都检查主进程及 8 个独立工具和收尾进程的 Job Object 成员身份，累计用户态 CPU 分别约 2.672 秒和 2.547 秒。两组用 48 MiB 限额复验，均记录内存超限、已停止和拒绝发布。早期运行因一次读取 231,056,136 B 的 Pandoc 程序计算摘要触发限额，改用分块读取后，仍核验同一个 SHA-256。两个隔离 PostgreSQL 库均已清除。 [SQLite 成功](../../tmp/ff-h18-windows-shared-zmhp5hq7/shared-windows-report.json) · [SQLite 超限](../../tmp/ff-h18-windows-shared-ub6p5nmo/shared-windows-report.json) · [PostgreSQL 成功](../../tmp/ff-h18-windows-shared-_ip2p505/shared-windows-report.json) · [PostgreSQL 超限](../../tmp/ff-h18-windows-shared-7gdcc7la/shared-windows-report.json)

WSL 与 Linux worker 的作业范围及额度另有原生实测。 [WSL 与 Linux worker](FF-H18-wsl-compute-budget.md)
