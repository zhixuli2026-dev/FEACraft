# 明确指定执行主机

N4-07 的远端路由接入既有 Redis delivery、SQL 唯一认领和 S3 封存输入流程。默认不指定目标，保持原先的本机执行规则。

## 配置与运行环境

在目标 worker 机器的服务端代码目录执行以下只读命令，取得真实主机标识：

```shell
python -c "from app.services.process_identity_native import host_id; print(host_id())"
```

提交端通过 `VIBEFLOW_JOB_EXECUTION_HOSTS` 指定运行时对应的目标主机，例如 JSON 对象 `{"windows":{"host_id":"目标机器输出的64位标识","platform":"linux"}}`。实际配置必须替换示例文字。

`windows` 是现有协议中宿主 shell 的历史名称：在 Windows worker 上使用 Windows shell，在 Linux worker 上使用 `/bin/sh`。它不将 Windows 命令或绝对路径自动改写为 Linux 命令。调用方应提交与明确指定的目标平台匹配的命令。`wsl`、`wsl-sandbox` 仍只接受 Windows 目标，并使用该 worker 的既有 WSL 配置；没有把它们静默转换成普通 Linux 命令。

生产部署中的 API 和 worker 使用同一 PostgreSQL 数据库、S3 命名空间和 Redis。worker 的本地缓存目录可以不同；封存输入从共享 S3 下载到 worker 自己的缓存，不要求共享 Windows 路径。跨主机配置在 FS 或 SQLite 模式拒绝提交。

在目标 worker 的进程环境中设置 `VIBEFLOW_DISTRIBUTED_JOBS=true`、既有 Redis 配置和 `VIBEFLOW_WORKER_HOST_QUEUE=true`，然后启动：

```shell
python -m app.services.distributed_worker
```

未迁移的旧任务仍使用原队列；保留普通 worker 处理旧队列。新指定主机的任务使用 `vibeflow:jobs:host:<host_id>`，避免不匹配的 worker 反复领取。没有对应 worker 时，SQL 中保留排队意图，不转交其他主机。

## 持久绑定

新指定主机的请求记录 delivery v2：提交主机、目标主机、目标平台、既有执行规格散列和批次信息。重投沿用原记录；修改环境配置不改写已经排队的意图。认领时再次核对目标主机和平台，然后沿用原来的 case 锁、配额、步骤绑定和唯一 claim。

正常关闭、中断、用户取消和收尾沿用 `worker-shutdown-drain.md` 的机制。远端 worker 身份来自实际操作系统进程观察，没有用配置值替代进程身份。

## 验证与剩余范围

显式入口 `platform/server/live_llm_remote_worker_acceptance.py` 使用 Windows API、实际非特权 Linux worker、隔离 PostgreSQL 数据库、实际 Redis/S3 和真实 DeepSeek；审查散列必须匹配才执行。证据目录为 `tmp/remote-worker-*`。该环境是同一物理 Windows 机器与 WSL 内的不同操作系统主机身份，不等同于独立物理主机的部署矩阵。

验收器需要本地 PostgreSQL 16。为允许本轮真实 WSL 连接，它仅临时添加当前测试数据库、当前角色、实际 WSL IPv4 `/32` 的 SCRAM 认证规则，结束后移除并记录原配置是否恢复。生产部署应由部署配置提供实际网络和数据库访问，不复用这个测试入口修改生产规则。

这一轮不覆盖所有专业 handler 的跨系统路径映射，也不关闭整个 N4-07。外部主机失联后的停止证明、恢复/接管、最终排空失败恢复和远端故障组合仍须按原计划完成。不能将远端连接失败或租约过期解释为进程已经停止。

## 2026-11-16 实测记录

最终远端证据为 `tmp/remote-worker-3j_jgmgl/remote-result.json`，PostgreSQL/访问规则清理为 `tmp/remote-worker-postgres-2d15b384d89e4fcd8873a6d64c41331e/`。4 个真实任务依次为成功、用户取消、停机中断、重启后成功；2 个 Linux worker 均以 UID 65534 运行并核验退出。真实 DeepSeek `deepseek-flash` 调用 2 次 HTTP 200，模型通过工具读回远端命令生成的随机文件。没有模拟模型。

默认旧队列回归为 `tmp/worker-shutdown-rz0dx2jj/`：FS/SQLite 下 5 个实际 WSL 任务、8 个 Windows worker，覆盖正常关闭、紧急关闭、宽限期耗尽、用户取消和重启；DeepSeek 实际调用 2 次 HTTP 200。新旧队列均使用审查过的显式 live_llm 入口。

未通过的尝试保留原始结果：`remote-worker-etzv1iiw` 被 PostgreSQL 本机访问规则阻止；`remote-worker-21bg_lc3` 缺少 Linux NumPy 等实际依赖；`remote-worker-xsc3_f0y` 已完成真实远端发布，但模型读回请求被 API 的 HTTP 409 拦截，尚未请求模型服务。该次旧错误记录未保存响应体，不能推断具体冲突原因。现已改进验收器，保留有界、脱敏的真实 HTTP 拒绝内容；后续原流程及默认队列回归通过，但这不构成对前一次 409 成因的证明，该诊断缺口保留到故障验收。所有尝试的测试数据库、对象服务和 Redis 均已清理；临时数据库访问规则已恢复。

Linux worker 环境位于 `/opt/vibeflow-worker-venv`，实际依赖版本保存在 `tmp/N4-07-linux-worker-packages-20261116.json`，`pip check` 通过。任务数据使用 Linux 自有 `/tmp/vibeflow-remote-worker-*` 缓存；这些隔离测试目录保留供检查。
