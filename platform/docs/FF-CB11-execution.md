# FF-CB11 算例执行设置

`caseDict` 的 `execution.workflow` 可保存注册工作流的求解方式。`solveMode` 支持 `serial`、`parallel`、`udf`。`parallel` 可声明 `subdomains`，省略时采用 `numericalMethod.decomposition` 已声明的数量，两处均未指定时沿用 2。`udf` 要求提供 `udfName`。新建工作流选择「按算例设置」时，从当前修订读取并在提交确认中显示实际求解方式。显式选择延续现有请求参数语义，数值分区冲突由原生输入检查拦截。

注册工作流和批次中的成员把同一配置解析成版本化计划，继续由当前的串行、MPI 和 UDF 处理器执行。重算请求省略参数时，采用当前算例设置并保留原计划和历史发布。现有 `execution.runMode` 字段，以及已经保存的作业和工作流读取格式沿用原协议。

SQLite 文件系统的 36 单元真实腔体已从声明自动选择两进程 MPI，完成分解、求解、重构和 D 确认。声明 3 个子域与分解块中的 2 个冲突时，保存返回 422 且修订保持。重放相同提交键确认只有一个工作流。两进程的 Y 范围为 0–0.05 m、0.05–0.1 m。 [文件系统实际回执](../../tmp/ff-cb10-decomposition-covcghd0/decomposition.json)

2026-11-23，PostgreSQL 和认证 SeaweedFS 的批次经相同声明完成真实 MPI 和 D 确认，返回相同的实际速度场摘要。批次键重放仍返回原批次。本机随后将 `execution.workflow` 从并行改为串行，重算请求未另传求解方式，新流程按当前声明实际串行求解并到 D，旧 MPI 发布和分区证据仍可读。 [认证对象存储批次](../../tmp/ff-cb10-decomposition-kz5q5ivd/decomposition.json) · [实际串行重算](../../tmp/ff-cb10-decomposition-w6jstry6/decomposition.json)

Edge 使用「按算例设置」读取当前修订，提交前显示 MPI 并行及两个子域。浏览器提交创建的实际工作流随后完成相同原生求解和 D 确认。 [界面提交回执](../../tmp/ff-cb10-decomposition-075gry8u/execution-browser.json) · [该流程的原生回执](../../tmp/ff-cb10-decomposition-075gry8u/decomposition.json)

同一配置机制的 `udf` 在两套存储完成实际 `wmake` 编译、库加载、`icoFoam` 求解和 D 确认。原生日志输出 `P0_UDF_OK`，执行 UID 为 65534，最终速度场摘要一致。遗漏 `udfName` 的声明在保存时返回 422。 [本地真实 UDF](../../tmp/ff-cb10-decomposition-wb3iw_q9/decomposition.json) · [认证对象存储真实 UDF](../../tmp/ff-cb10-decomposition-h_1gf2pu/decomposition.json)

SQLite 文件系统和 PostgreSQL 认证 SeaweedFS 的报告工作流从同一并行声明开始，贯通实际 MPI 导热、后处理和报告。后处理固定求解发布，报告固定后处理发布，两处平均温度均回算为 350 K。D、E 均经明确确认，HTML 和 DOCX 下载字节均记录摘要。对象存储首轮报告发布后立即读取时返回 `file_sync_busy`，复验等候收尾后完成下载。 [本地完整报告链](../../tmp/ff-cb10-numerical-fu04rl3m/numerical.json) · [对象存储完整报告链](../../tmp/ff-cb10-numerical-5x3k1mhq/numerical.json) · [首次读取回执](../../tmp/ff-cb10-numerical-5qpufmta/numerical.json)

`execution.interactive` 可以声明 `shell cmd`、`wsl` 或 `wsl-sandbox`。终端面板使用当前算例的默认环境，用户仍在点击启动时创建会话，也可手动选择。Edge 在 SQLite 文件系统和 PostgreSQL 认证 SeaweedFS 实测默认普通 WSL，实际 `id -u` 均返回 65534，关闭后持久记录进程退出。切换到 Windows 选项时尚未创建第二个会话。非法环境在保存时返回 422。较早的测试记录了命令逐字符回显延迟，以及原生 UID 前的 ANSI 控制符；最终解析终端控制序列后确认原生结果及关闭。 [文件系统终端](../../tmp/ff-cb11-interactive-vu4x5xnk/interactive-acceptance.json) · [认证对象存储终端](../../tmp/ff-cb11-interactive-p6o4dw1a/interactive-acceptance.json) · [早期实际控制序列](../../tmp/ff-cb11-interactive-peomtjfs/interactive-browser-reads.json)

Web 构建产物写入 D 盘。构建前后 C 盘分别可用 14,524,948,480 B、14,513,762,304 B，Docker 内部回收为 0 B，11 个容器运行，39 个持久卷在用。 [Web 构建记录](../../tmp/ff-cb11-web-build-20261122.log)

## 远程求解路由

`execution.runMode remote` 使用已配置的 Redis worker 路由。工作流计划保存远程放置要求，串行、MPI 或 UDF 方式按原配置选择。实际派发求解与 UDF 编译时，目标来自部署的 `VIBEFLOW_JOB_HANDLER_EXECUTION_HOSTS` 或 `VIBEFLOW_JOB_EXECUTION_HOSTS`。创建工作流时，缺少所需路由会返回 `runtime_unavailable`。几何、网格、后处理和报告沿用各自的部署路由。 [执行声明](../server/app/services/case_execution_settings.py) · [部署路由](../server/app/services/job_execution_hosts.py)

SQLite 文件系统经 Edge 提交实际两进程 MPI。普通队列 worker 已在运行，目标 worker 启动前，求解在指定队列等待且数据库无领取记录。启动指定 worker 后，它取得原尝试，完成分解、求解、重构和 D 确认。速度场摘要与已有本地原生结果一致。两个 worker 和隔离 Redis 均已退出。本次使用同一物理宿主。 [Edge 提交](../../tmp/ff-cb10-decomposition-ltgowbaq/execution-browser.json) · [原生求解](../../tmp/ff-cb10-decomposition-ltgowbaq/decomposition.json) · [缺路线拦截](../../tmp/ff-cb11-remote-missing-route-sf8hendy/remote-missing-route.json) · [Redis 停止证明](../../tmp/ff-cb11-target-redis-7o0idmat/redis-stops.json)

PostgreSQL 和认证 SeaweedFS 的批次用相同声明运行。启动指定 worker 前，实际求解没有被普通队列领取。指定 worker 完成两进程 MPI，结果摘要与文件系统一致，重复批次键返回原批次，D 已确认，临时 PostgreSQL 库已清除。 [对象存储批次](../../tmp/ff-cb10-decomposition-cb5odq5r/decomposition.json) · [隔离库清理](../../tmp/terminal-agent-postgres-peo2jyud/cleanup.json)

文件系统从远程 MPI 改为串行后，重算使用当前算例修订。新求解由普通 worker 领取，旧 MPI 发布仍可按版本读取。串行作业的显式时刻、最近完整检查点、禁用限制与归档恢复已有实测。 [切换方式并重算](../../tmp/ff-cb10-decomposition-prfzs6e7/decomposition.json) · [续算策略与证据](FF-C13-restart-policy.md)

## 2026-11-23 验收

无附件的工程报告曾因资源选择为空停在准备阶段。修复后，真实 CAD、网格、求解和后处理贯通 B、C、D、E，并生成 HTML 与 DOCX。五类产物均核对原 `caseDict` 修订、封存摘要和前级发布。真实 DeepSeek 回读 2 次 HTTP 200，模拟 0。 [首次超时](../../tmp/live-cli-startup-8wvalv61/live-result.json) · [真实产物来源](../../tmp/live-cli-startup-r994s9te/registered-artifact-trace.json) · [模型回执](../../tmp/live-cli-startup-r994s9te/live-result.json)

FF-CB11 按 T02 验收。跨物理宿主保留在 N7-01，FF-C03 的 HPC、SSH 和 Slurm 执行继续跟踪。 [FF-CB11 完成回执](../../tmp/FF-CB11-execution-20261122/acceptance.json) · [本机远程路由矩阵](../../tmp/FF-CB11-execution-20261122/remote-validation.json)

## FF-C03 T04 验收

远程串行 OpenFOAM 的检查点续算沿用 `solve.openfoam` 的主机路由。部署可单独指定 `solve.openfoam.resume` 覆写。SQLite 文件系统和 PostgreSQL 认证 SeaweedFS 各启动普通和指定 worker。真实求解在指定队列等待，领取后取消并核验原生进程退出。新流程在同一指定队列从 0.05 s 续算到 0.5 s，确认 D。重复恢复键返回同一流程，两套存储的 `U`、`p` 与从头原生计算逐字节一致。 [路由实现](../server/app/services/job_execution_hosts.py) · [文件系统](../../tmp/ff-c03-remote-checkpoint-qygr4t8x/remote-checkpoint.json) · [认证对象存储](../../tmp/ff-c03-remote-checkpoint-5klohk2t/remote-checkpoint.json)

同一物理机的隔离 Docker 环境以 OpenSSH 公钥认证连接实际 Slurm 节点。SFTP 传入七个 OpenFOAM 输入，`sbatch` 调度后由 UID 1000 完成 `checkMesh` 和 `icoFoam`。`scontrol` 返回 `COMPLETED`，SFTP 回收的两份场与独立运行逐字节相同。实际执行过的两次错误分别为镜像缺少 OpenFOAM 和在初始化 bashrc 时提前开启 `set -e`，修正后复验通过，隔离容器已移除。Slurm／PBS 的持久管理继续在 FF-C16 交付，第二台物理宿主验收保留在 N7-01。 [Slurm 回执](../../tmp/FF-C03-local-scheduler-20261123/vf-slurm-acceptance-6a3850c8e1/receipt.json) · [真实原生日志](../../tmp/FF-C03-local-scheduler-20261123/vf-slurm-acceptance-6a3850c8e1/slurm-job.log) · [镜像和磁盘](../../tmp/FF-C03-local-scheduler-20261123/openfoam-build-disk.json)

FF-C03 按 T04 汇总串行、MPI、批量、交互、远程执行、持久作业和归档恢复。远程续算及 Slurm 实算真实模型 0 次、模拟 0 次。 [T04 验收](../../tmp/FF-C03-local-scheduler-20261123/t04-validation.json) · [台账](../../tmp/stage-ledger-evidence-20261123-ff-c03.json)
