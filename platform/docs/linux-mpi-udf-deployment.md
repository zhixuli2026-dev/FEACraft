# Linux MPI 与 UDF 部署验收

2026-11-18。延续[Linux 串行工程部署](linux-cae-deployment.md)，正式 MPI 和 functionObject UDF 处理器现已使用同一 OpenFOAM 容器运行。MPI 完成分解、两进程求解、重构；UDF 完成真实 C++ 编译、ELF 加载、求解器集成。两条流程均经过公共工作流的工程确认、后处理和 HTML/DOCX 发布。

## 配置与使用

仍使用 `infra/Dockerfile.api`、`infra/Dockerfile.cae` 和 `infra/docker-compose.cae.yml`。API 基础镜像现在包含 `server/examples`，使 `/cases/{id}/udf/scaffold` 能读取实际模板。API 与 worker 应使用相同代码及工具链版本，构建和持久卷配置见串行部署说明。

工作流参数 `solve_mode=mpi`、`subdomains=2` 选择正式 MPI；`solve_mode=udf`、`udf_name=myFunctionObject` 选择已创建的 functionObject。几何、网格、物理、求解时间和确认条件仍由工程算例及 caseDict 提供。本次没有新建另一个调度入口。

Linux 配置使用 `distro=native-linux` 和实际 OpenFOAM bashrc；Windows 保留原 WSL 发行版与非特权用户。本次 Linux Docker 沿用既有容器 UID 0，只为该次真实 MPI 启动设置 OpenMPI 的两项 root 运行环境变量。Windows/WSL 实测 UID 65534，未放开普通 WSL root。两种平台继续使用原有受控子执行、取消和停止证明；这不代表已交付每用户非 root 容器隔离。

公共 `/solve/results` 现在按已封存的处理器读取 MPI/UDF 对应求解证据及原生工具链，不再只寻找串行求解文件。历史所有权、输入过期和文件一致性检查继续适用。

## 实际验收

使用实际 400 单元二维 cavity 输入，真实 `icoFoam` 求解至 0.5。串行参考来自 `linux-2c1d7807af` 中的真实速度场；复用前校验全部输入和字段 SHA-256。没有固定数值回复或模拟求解器。

| 场景 | 已验证内容 |
| --- | --- |
| Linux 与 Windows/WSL MPI | 真实 decomposePar → 2 个 MPI 求解进程 → reconstructPar → 后处理 → HTML/DOCX 下载。速度场对串行参考的相对误差约 `1e-6`。 |
| Linux 与 Windows/WSL UDF | 唯一标识写入实际 C++，wmake 生成新 ELF，加载器成功，真实求解日志出现该标识，最终速度场与串行参考一致；实际后处理和报告下载通过。 |
| 编译依赖 | 实际编译器、链接器依赖记录和有界 strace 查询均保留；Make/files 与 Make/options 两个实际预处理命令及依赖查询都通过。 |
| MPI 运行中取消 | 先观察对应 PID 命名空间中 2 个真实 icoFoam 进程，再调用公共工作流取消；作业 67 cancelled，根/子停止凭据有效，原命名空间无剩余进程。 |
| UDF 运行中取消 | 实际编译后观察真实 icoFoam 进程，再取消；作业 118 cancelled，根/子停止凭据有效，原命名空间清空。该场景不声称已有可续算检查点。 |
| 重建后读回 | 仅重建验收 API/worker，确认容器身份改变；MPI/UDF 历史报告各文件下载 SHA-256 保持，求解结果仍未过期；两容器的全部 app Python 源码与工作区匹配。 |

选定验收共 8 组：串行参考、双平台 MPI、双平台 UDF、两类运行中取消及重建读回。按算例去重计 39 个实际作业，37 成功、2 按请求取消；原生停止凭据全部核验。旧重试和失败算例不计入这 39 个作业。真实模型调用 0、模拟 LLM 0；本轮属于工程工具与基础设施验收，不替代 Agent 模型回合验收。

MPI 平均压力为 `0.022259352333744136`，UDF 为 `0.02226855869812202`，差约 `9.21e-6`，在本次 `1e-5` 判据内；不声称二者压力完全相等。

## 修复与保留的失败

1. 最初 Linux 实际编译成功，但 GNU make 4.4.1 默认没有输出更新已包含 Make/options 时的命令，依赖检查拒绝发布。加入 `--debug=v,m --trace` 后，两条预处理命令均来自真实执行；没有放宽“必须两条”的校验。中间误写为 `--debug=vm`，只启用了第一个选项，失败原件保留。参数分隔规则见 [GNU make 官方选项说明](https://www.gnu.org/software/make/manual/html_node/Options-Summary.html)。
2. 编译依赖通过后发现 Docker Python 镜像没有 `/usr/bin/python3`。Linux 加载器改为实际 `sys.executable`，并记录其路径和文件哈希；Windows/WSL 保持原路径。
3. 一次 Windows 定向复验误引用没有串行字段的证据目录，在提交工程前失败；改为已保存的真实 Linux 串行参考。没有生成替代字段来让测试通过。

Windows 最终完整验收在审查版本 v4；之后唯一生产差异是 Linux 专用加载器选择，封存脚本核对该差异及其余 app 源码一致，没有把它表述为另一次最终版本 Windows 实跑。Linux 最终完整流程和取消使用 v5，重建读回使用 v6。所有审查源码及中间失败均保留。

## Linux 通用命令与 UDF 隔离

FF-J16 将 Linux 宿主的通用命令、类型化命令和命令批次接入独立的挂载、PID 和网络命名空间。工作目录为本次封存的可写副本，作业继承有限的工具和区域配置。私有回环、POSIX 共享内存、HOME 和临时目录供本次执行使用。API 继续使用已有 `runtime=windows` 字段选择 Linux 宿主上的原生入口。旧任务可按已有流程恢复封存输入，或使用当前版本重新提交。

UDF 协调器沿用已登记的作业和子进程通道。运行 `wmake`、ELF 加载器及实际求解器的子进程进入 OpenFOAM 的隔离目录和私有网络。只读应用源码、清单及工具链用于构建和追踪，原生子进程的停止凭据绑定到该次执行。

2026-11-26，文件系统与认证 SeaweedFS 共完成 12 个通用命令作业，覆盖全量封存、显式选取文件、类型化发布、发布引用和批次。普通 worker 可访问的测试文件、环境变量和 Redis 在作业内隔离，作业内真实回环通信和 POSIX 共享内存通过。两个非特权宿主 worker 分别计算了 400 单元串行参考，编译实际 ELF，并由 `icoFoam` 加载 UDF。两份归档速度场对各自串行参考的归一化最大差均为 `4.99e-7`。两次取消均以实际 `icoFoam` 已启动为前提，父、子进程已核验停止，原速度场保留。S3 使用起初无缓存的对端 API，跨账户查询返回 404。模型调用 0 次。 [文件系统回执](../../tmp/j16-command-udf-ta5augir/result.json) · [认证 S3 回执](../../tmp/j16-command-udf-oyljoxx0/result.json)

验收环境已清理，原 11 个 Docker 服务标识一致。构建缓存仍为 5.857 GB。Windows C 盘可用空间由 39,216,209,920 B 变为 39,211,876,352 B。

复验使用现有 `vibeflow/api:linux-mpi-udf-v4-20261118` 镜像和真实 PostgreSQL、Redis、OpenFOAM。

```powershell
& platform/server/.venv/Scripts/python.exe -X utf8 -B platform/server/linux_command_udf_isolation_acceptance.py --storage fs
& platform/server/.venv/Scripts/python.exe -X utf8 -B platform/server/linux_command_udf_isolation_acceptance.py --storage s3
```

## 证据与后续范围

证据位于 `tmp/linux-mpi-udf-20261118/`：

- `linux-2c1d7807af`：实际串行参考、MPI 报告及首次 UDF 失败。
- `windows-a63c67fa7f`：Windows/WSL MPI 与早期 UDF 完整通过；`windows-2e9e44c618`：最终 Windows UDF 完整通过。
- `linux-6ed9c9c2fc`：Linux UDF 编译、加载、集成和报告完整通过。
- `linux-96f67eaef8`：MPI 取消通过，随后 UDF 失败原件保留；`linux-9c2d138c3c`：UDF 取消通过。
- `actual-udf-build-native.json`、`restart-result.json`：实际编译凭据、重建与文件读回结果。

汇总为 `tmp/mainline-linux-mpi-udf-evidence/validation.json`。本阶段 S3 验收部署（57700）使用 `vibeflow/api:linux-mpi-udf-v4-20261118`，后续镜像见 [Gmsh 部署说明](linux-gmsh-deployment.md)；FS 部署（62883）仍使用上一版工程报告模板镜像。更新与回退沿用串行部署手册，等待作业停止后更换 API/worker，保留数据卷。

本次覆盖 functionObject UDF 和既有正式 MPI 工作流；自定义求解器、边界条件、源项等全部 UDF 类型、其他外部工具、物理多主机及完整部署条件仍按原计划推进。没有把同主机独立容器当成物理多主机验收。

主线 **49/53，剩 4 项**（N4-05、N7-01、N7-05、N7-06）；资源 **11/17，剩 6 块**；全功能 **2/295，剩 293 项**。N7-05 保持进行中，原 53/295 范围不变。
