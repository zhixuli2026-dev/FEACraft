# Linux 容器内的工程流程

在工程镜像上追加三引擎文档编译和源码定位见 [Linux TeX 部署](linux-tex-deployment.md)。

后续 MPI 与 functionObject UDF 见 [Linux MPI/UDF 部署验收](linux-mpi-udf-deployment.md)；正式 Gmsh 网格来源与真实模型读回见 [Gmsh 部署验收](linux-gmsh-deployment.md)。下文保留串行阶段的原始结果和镜像版本。

2026-11-18。原 Docker API 能生成报告，但正式网格、求解和续算处理器要求 `wsl.exe`，Linux 部署无法完成工程链。现在按宿主系统选择执行方式：Windows 保留已有 WSL，Linux 使用 `/bin/bash` 和本机 OpenFOAM。复用已有进程执行、停止证明和注册处理器；没有另建调度器或降低取消验收条件。

## 部署

从仓库的 `platform` 目录先构建基础 API，再构建包含 OpenFOAM 的镜像。生产密钥、数据卷和分布式设置仍按 [部署说明](deployment.md)配置。

```sh
docker build -f infra/Dockerfile.api -t vibeflow/api:dev .
docker build -f infra/Dockerfile.cae --build-arg API_IMAGE=vibeflow/api:dev -t vibeflow/api:cae .
docker compose --env-file infra/.env -f infra/docker-compose.prod.yml -f infra/docker-compose.cae.yml --profile distributed up -d
```

分布式执行仍需在部署环境设置 `VIBEFLOW_DISTRIBUTED_JOBS=true`。只启用 profile 不会自动切换 API 的调度方式。API 和 worker 必须使用同一代码和工具链版本。

`Dockerfile.cae` 使用 OpenCFD 签名软件源，默认安装 `openfoam2406-dev=2406.260127-2`、strace，以及后续新增的 Debian `gmsh=4.13.1+ds1-6`。构建参数 `OPENFOAM_SERIES`、`OPENFOAM_VERSION`、`GMSH_VERSION` 可指定实际目标版本；更换版本后须重新验证业务，不能复用本次结论。镜像设置 `VIBEFLOW_OPENFOAM_BASHRC=/usr/lib/openfoam/openfoam2406/etc/bashrc`，按原规则优先于 Setup 保存值。

Compose 覆盖文件支持 `VIBEFLOW_API_IMAGE`（基础镜像）和 `VIBEFLOW_CAE_IMAGE`（包含工具链的镜像）。它保留原生产配置的持久卷、worker 命令、密钥挂载及进程命名空间选项。实际默认值和自定义镜像参数渲染均已检查。

登录后请求 `/api/v1/runtime/capabilities`。为保持接口兼容，原生 Linux 结果仍位于既有 `wsl` 字段，但 `distro` 明确为 `native-linux`；必须同时核对 `reachable`、`openfoam`、`wmake` 和工具版本。旧字段名不表示容器里运行了 WSL。执行规格中的宿主运行时历史名称 `windows` 在 Linux 上使用已有 Linux 执行机制；停止证明核验实际系统身份。

## 已完成的实际验收

| 场景 | 结果与证据 |
| --- | --- |
| Linux 工具探测 | 真实用户归属的 Python、OpenFOAM/wmake 请求成功；OpenFOAM v2406。 |
| 完整 S3 工程链 | 独立 API/worker 容器、PostgreSQL/Redis/SeaweedFS，正式 CadQuery → blockMesh/checkMesh → laplacianFoam → VTK → Pandoc，B/C/D/E 确认均经过公共接口。80 单元实际坐标/温度文件独立计算，解析解 `T=300+100*x` 的 L2 和最大误差均为 0 K；HTML/DOCX 等发布文件按 SHA-256 下载核验。 |
| 运行中取消 | 实际 icoFoam 写出 `0.005/U` 和 `0.005/p` 后，确认作业仍运行，再经工作流取消。作业 27 为 cancelled，完整停止证明有效。 |
| 检查点续算 | 对作业 27 的原封存输入和真实检查点调用 `:repeat`，新作业 28 从 0.005 后继续到 0.5 并正常结束；停止证明有效。 |
| Windows/WSL 兼容 | 原完整热传导流程通过；运行记录修正后另外执行真实 Python/WSL 能力探测，2 个请求及其子执行正常结束，临时 API 退出。 |
| 容器重建后恢复 | 实际重建 S3 API 和 worker，容器身份均已改变；原报告的全部发布文件 SHA-256 保持，续算结果仍正常且未过期，原生工具探测再次成功。两容器各 466 个 Python 源码文件与工作区一致。 |

Linux 共 11 个实际作业：10 成功、1 按请求取消，所有原生停止凭据核验。Windows 热传导另 6 个实际成功作业。本轮模型调用 0、模拟 LLM 0；这是实际工程与基础设施验收，不能替代 Agent 模型验收。

首轮 `acceptance-08812c9caf` 暴露原生 Linux 请求仍被记录器按 WSL 校验的问题，修正运行时与发行版绑定后解决。`acceptance-a20b45c5d3` 已通过完整工程链，但脚本在取消后读取暂未生成的停止凭据时误对空值调用 `.get()`；失败原件保留。修正脚本后，`acceptance-57c5d3c895` 直接续算原作业，未重跑已通过的完整工程链。最终结论使用两份功能证据及全部作业的独立停止核验，不声称首轮整套脚本通过。

原始证据均在 `tmp/linux-cae-20261118/`；Windows 工程为 `tmp/cae-registered-conduction-vaas8o_9/benchmark.json`，Windows 探测为 `tmp/linux-cae-windows-probe-m_jbu_ut/compatibility.json`。审查源码归档为 `tmp/sources-N7-05-linux-cae-v1-20261118.zip`、`tmp/sources-N7-05-linux-cae-v3-20261118.zip` 和 `tmp/sources-N7-05-linux-cae-v4-20261118.zip`；v3 到 v4 只修正验收脚本。实际镜像基于完整 v1 工具链构建，v3 使用代码层修正，保留构建日志。

本节串行 S3 验收（57700）使用 `vibeflow/api:linux-cae-v3-20261118`，后续镜像见 MPI/UDF 部署记录；FS 验收部署（62883）继续运行上一版工程报告模板镜像，本次没有把它记作 Linux 求解验收环境。Windows 临时 API 由测试工具终止后退出码为 1，只证明进程已退出；正常原生请求结束由其数据库停止记录单独证明。

## 操作、迁移和回退

提交新工程前先检查实际工具能力。取消通过工作流/作业 API 操作；等停止证明就绪后，再选择 `operations.resume` 和明确的已写出时间步续算。续算属于新作业，不改变原取消状态；没有完整检查点或输入已经变化时应按现有接口拒绝。

Linux 检查点绑定 `native-linux`，Windows/WSL 检查点绑定原发行版。两者不可直接跨运行时续算；迁移到另一运行时应保留原历史，以当前目标重新准备并运行。工具路径、版本、数据、数据库和 provider 密钥按原恢复手册共同保留。

更新镜像前停止新提交并等待作业结束，备份数据库、算例与部署配置。使用目标镜像重建 API/worker；持久数据留在原卷。回退时恢复匹配的镜像和配置；旧镜像不支持本次 Linux 求解入口，不能继续提交依赖此功能的新任务。禁止用 `down -v` 作为更新或回退步骤。

## 尚未完成的范围

本节证明 Linux 串行 OpenFOAM 工程链和检查点续算。后续 MPI/functionObject UDF、正式三维 Gmsh 验收单独记录；后续 TeX/SyncTeX 与 FloTHERM 辅助库已分别完成 Linux 入口及实际验收，见 [TeX 部署](linux-tex-deployment.md)和 [FloTHERM 部署](linux-flotherm-deployment.md)。各文档保留具体覆盖边界，不能由工具安装推定全部功能已验收。物理多主机、完整外部依赖、存储条件操作兼容和全功能交付继续按原任务推进。同主机的独立容器不冒充物理多主机。

主线 **49/53，剩 4 项**；资源 **11/17，剩 6 块**；全功能 **2/295，剩 293 项**。N7-05 保持进行中，53/295 原范围不变。
