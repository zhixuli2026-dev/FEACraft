# FF-H18 WSL 作业 CPU 和内存用量

Windows 宿主上普通托管 WSL 命令可使用 `VIBEFLOW_EXECUTION_WSL_CPU_LIMIT_SECONDS` 和 `VIBEFLOW_EXECUTION_WSL_MEMORY_LIMIT_MB`。默认值为 `0`。启用后，配置随尝试封存，启动器先加入原有进程生命周期控制组，再加入私有内核资源组，最后才按原启动协议运行脚本。累计 CPU 时间包含用户态和内核态，启用 CPU 限额时从同组读取累计计量并限制 CPU 调度速率。内存设置同时限制驻留内存及内存与交换空间之和。 [WSL 资源模块](../server/app/services/native_compute_v1.py) · [启动绑定](../server/app/services/job_native_scopes.py) · [Linux CPU 计量](https://docs.kernel.org/admin-guide/cgroup-v1/cpuacct.html) · [Linux 内存组](https://docs.kernel.org/6.6/admin-guide/cgroup-v1/memory.html)

本机 WSL 仍用 cgroup v2 管理原进程停止，CPU、CPU 计量和内存控制器由 cgroup v1 单独提供。模块通过私有挂载命名空间访问控制器。实际分配测试中，48 MiB 上限以下的两个真实子进程可运行。强制触碰 96 MiB 的进程触发原生 OOM 终止，`memory.memsw.max_usage_in_bytes` 为 50,331,648，OOM 计数为 1。最初只限制驻留内存时，额外内存经交换空间使用，所以当前同时限制两类计量。 [原生内核用量](../../tmp/FF-H18-wsl-compute-20261122/native-combined-probe.log) · [模块真实验收](../../tmp/FF-H18-wsl-compute-20261122/native-adapter.log)

两条脱离会话的子进程累积到约 0.522 秒 CPU 后，已有 cgroup v2 生命周期接口确认目标进程树退出并移除作用域。首次正式内存 OOM 的输出收尾只接收 Windows 来源，测试等待超时后结束该算例。补齐后，Windows 侧采样的 CPU 停止仍在约 3.5 秒触发。正式 WSL 启动器改为在本机每 20 ms 读取内核计量，通过原 PID 命名空间停止整组。 [整组 CPU 停止原生试验](../../tmp/FF-H18-wsl-compute-20261122/native-cpu-stop.log) · [首次收尾问题](../../tmp/ff-h18-wsl-compute-jgxa81bb/wsl-compute.json) · [宿主延迟实测](../../tmp/ff-h18-wsl-compute-haszl4gt/cpu-result.json)

SQLite 与 PostgreSQL 的正式 WSL 作业均通过普通命令、内存 OOM、CPU 超限和真实停止。1 秒 CPU 限额的实际总量分别为约 1.041 秒和 1.052 秒。API 重启后用量可读，显式重试保留原限额，新作业使用更新值。两个独立会话的子进程退出已核验。 [SQLite 实测](../../tmp/ff-h18-wsl-compute-5q06mpcg/wsl-compute.json) · [PostgreSQL 实测](../../tmp/ff-h18-wsl-compute-auqybdic/wsl-compute.json)

同一限额接入了已注册的 WSL 工作流。SQLite 和 PostgreSQL 加认证 SeaweedFS 均以 UID 65534 运行 Gmsh 和 OpenFOAM，在 20 秒累计 CPU 与 512 MiB 内存上限内，完成 64 单元传热并确认 D。两个作业的用量均持久化，温度解析误差为 0。共享启动器的独立工具回归使用真实 DeepSeek `deepseek-flash`，2 次 HTTP 200，模拟 0。FF-H18 的独立 Linux worker 范围继续。 [托管文件系统](../../tmp/ff-b18-gmsh-artifacts-klr0e7bb/mesh-artifacts.json) · [托管认证对象存储](../../tmp/ff-b18-gmsh-artifacts-4fgpy_tr/mesh-artifacts.json) · [完整验收](../../tmp/FF-H18-wsl-compute-20261122/validation.json)

2026-11-23 00:09，读取独立 Docker worker 的原生控制组挂载状态。该 Linux worker 的 `cpu`、`cpuacct` 与 `memory` 控制器均为只读，Docker 配置没有附加能力或独立控制组父目录。它目前可作为目标环境的资源控制条件证据。独立 worker 的每作业 CPU 与内存额度验收仍待可写的独立控制组。

## Linux worker 的原生作业额度

在提交 API 配置 `VIBEFLOW_EXECUTION_LINUX_CPU_LIMIT_SECONDS`、`VIBEFLOW_EXECUTION_LINUX_MEMORY_LIMIT_MB`，默认均为 `0`。额度随尝试冻结，显式重试沿用旧额度。当前已验证 Docker 的 cgroup v1 私有命名空间。固定启动门通过私有挂载写入额度，作业继承的控制组挂载保持只读。单进程树由原启动门负责终止和计量。注册工具作业由同一尝试内的主进程、工具进程和收尾验证使用同一控制组，最后一批验证结束并核实清空后记录总量。作业详情提供 `attempt.linux_compute_usage`。 [启动门实现](../server/app/services/linux_gate_compute.py) · [Docker 容器参数](https://docs.docker.com/reference/cli/docker/container/run)

在现有 Docker 镜像上，用临时容器完成两次正式作业验收。SQLite 与 PostgreSQL 加真实 Redis 的独立 worker 均覆盖普通命令、两个后台子进程的总 CPU、超过内存阈值、运行中取消和停止证明。1 秒 CPU 上限分别在实际累计约 1.016 秒和 1.025 秒停止。48 MiB 内存和交换空间上限均触发一次 OOM，显示峰值 50,331,648 B。重启 API 后原用量保持，重试保留 1 秒和 48 MiB，新作业采用 4 秒和 128 MiB。独立 worker 的 PID 与数据库中的领用身份一致。 [SQLite 回执](../../tmp/FF-H18-wsl-compute-20261122/linux-container-evidence/ff-h18-linux-compute-g0ntlpob/linux-compute.json) · [PostgreSQL 和 Redis 回执](../../tmp/FF-H18-wsl-compute-20261122/linux-container-evidence/ff-h18-linux-compute-dxxiq3qp/linux-compute.json) · [启动器原生回执](../../tmp/FF-H18-wsl-compute-20261122/linux-docker-gate.log)

在 SQLite 和 PostgreSQL 加真实 Redis worker 上，正式 Pandoc 报告分别经实际 CPU、内存控制组验收。两组低额度测试均在 48 MiB 触发一次内核 OOM，原生进程退出且报告未发布。4 秒和 512 MiB 配置下，两组主进程、8 个独立工具与收尾进程均核对实际内核入组和停止，组在结算后移除。累计 CPU 约 3.474 秒和 2.671 秒，内存峰值约 263 和 265 MiB，认证下载的 HTML、DOCX 均核对内文。Windows 默认配置另做 5 类实际故障回归及真实 DeepSeek 工具回读 2 次 HTTP 200，模拟 0。 [SQLite 共用额度](../../tmp/FF-H18-wsl-compute-20261122/linux-container-evidence/ff-h18-linux-compute-neh2eouh/linux-compute.json) · [PostgreSQL 和 Redis 共用额度](../../tmp/FF-H18-wsl-compute-20261122/linux-container-evidence/ff-h18-linux-compute-mlitiuyw/linux-compute.json) · [真实模型回执](../../tmp/live-cli-startup-wy8ti4_d/live-result.json)

隔离验收使用 Linux tmpfs 满足原子重命名和默认节点空间预留要求。首次 Windows 共享盘的原子重命名返回 `EINVAL`，256 MiB tmpfs 也不足以保留默认 256 MiB 空闲额，改用 768 MiB tmpfs 后通过。临时 API、Redis 和 PostgreSQL 容器已退出。Docker 内部回收量为 0 B。C 盘核对点 02:07 的可用量为 12,472,004,608 B，02:28 为 12,481,413,120 B。定时维护下一次为 03:00。 [共享盘失败](../../tmp/FF-H18-wsl-compute-20261122/linux-container-evidence/ff-h18-linux-compute-cftcks_4/api.log) · [Docker 维护日志](../../tmp/docker-space-maintenance.jsonl)

共享执行器更改后，真实 Windows 故障回归覆盖退出码、日志、墙钟、工作目录空间与磁盘预留，随后用实际 DeepSeek 工具读回核对结果，2 次 HTTP 200。实际 WSL Codex 通过显式配置本机 Linux 程序完成 1 个模型回合并原样读回文件，读取回执含原生命令、持久答复和会话停止。该回合未逐次记录 CLI 内部 HTTP 请求。模拟调用为 0。 [Windows 回归](../../tmp/live-cli-startup-w3adw9m5/job-resource-faults.json) · [真实模型回执](../../tmp/live-cli-startup-w3adw9m5/live-result.json) · [WSL CLI 实测](../../tmp/live-cli-inference-a2e_6wul/live-result.json)

## 作业用量界面

作业详情显示已封存的 CPU 与内存限额、实测用量和超限类型。Edge 经冷启动 API 读回实际 WSL 超限作业，在 1440、900 和 640 像素宽度均可读取 1 秒和 48 MiB 限额、实际峰值与停止原因。API 的独立 Linux worker 用量已在上节实测。 [Edge 回执](../../tmp/cb08-h18-edge-wsl-s14zdetd/browser.json) · [640 像素内存用量](../../tmp/cb08-h18-edge-wsl-s14zdetd/memory-640.png)

03:00 定时清理正常完成，Docker 缓存与悬空镜像各回收 0 B。Windows C 盘可用量由 11,763,408,896 B 变为 11,763,273,728 B。下一次定时维护为 09:00。 [定时维护日志](../../tmp/docker-space-maintenance.jsonl)

## 注册 WSL 工具

通过 WSL 启动的注册工具在同一尝试内累计 CPU。开启任一 WSL 额度后，工具进程范围依次执行，下一范围从原额度扣除已经核验的 CPU 时间，并沿用同一内存限额。每个范围内部的 MPI 等真实并行进程仍共享内核限额。结算时跨范围加总 CPU，记录内存峰值和 OOM 次数。 [原生范围记录](../server/app/services/job_native_scopes.py)

SQLite 文件系统和 PostgreSQL 加认证 SeaweedFS 均完成两进程 MPI，速度场摘要一致，D 已确认，累计 CPU 分别约 4.505 秒和 4.438 秒。LaTeX 的三次原生调用按顺序扣减额度，累计 8.166 秒，最终发布实际 PDF。负例使用 3 秒限额，两次调用单独都低于 3 秒，合计 3.030 秒触发停止及拒绝发布。 [SQLite MPI](../../tmp/ff-cb10-decomposition-1v3d4dkk/decomposition.json) · [认证对象存储 MPI](../../tmp/ff-cb10-decomposition-dbmee_io/decomposition.json) · [LaTeX PDF](../../tmp/ff-h18-wsl-latex-xoqqcekd/registered-latex-compute.json) · [累计 CPU 超限](../../tmp/ff-h18-wsl-latex-gib_1kb1/registered-latex-compute.json)

## 2026-11-23 验收

FF-H18 完成 T01。Windows、WSL 和当前 Docker Linux 组合均有原生 CPU、内存和作业停止证据。SQLite 与 PostgreSQL 各完成 201 项积压、公平调度、批次取消和额度复用。作业用量通过 Edge 核验。本项验收关联 14 次真实 DeepSeek HTTP 200，另 1 个真实 WSL CLI 模型回合，模拟 0。跨物理宿主仍在 N7-01 跟踪。 [完成回执](../../tmp/FF-H18-wsl-compute-20261122/acceptance.json) · [Windows 实测](FF-H18-windows-compute-budget.md) · [公平队列](fair-queue-and-batch-reconciliation.md#201-项积压验收) · [最新真实模型回执](../../tmp/live-cli-startup-cu4mrnoj/live-result.json)
