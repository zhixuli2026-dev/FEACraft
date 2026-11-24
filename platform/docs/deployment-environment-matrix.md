# 部署环境与证据矩阵

2026-11-18 当前本机部署的能力清单。矩阵区分已安装、已实际验收的业务范围及仍由原功能任务承接的扩展；不是全部 295 项完成声明。

## Linux 工程栈

当前 S3 项目为 `vf-report-s3-20261117`，Web 仅监听 `127.0.0.1:57700`。API/worker 为 `vibeflow/api:cli-discovery-20261118`；镜像 ID 与源码、实际命令和模型证据见 [F54 完成审查](FF-F54-completion-review.md)。另一个 FS 项目位于 `127.0.0.1:62883`，仍保留之前的报告工程镜像，不把它说成拥有当前 S3 工程镜像的全部新增工具。

| 组件 | 当前配置/安装方式 | 已验证业务范围 |
|---|---|---|
| 主机与 API | Docker Desktop 的 WSL2 Linux 内核；Debian 13、Python 3.11；`infra/Dockerfile.api` | PostgreSQL/Redis、S3、独立 worker、认证 API、健康与真实模型 |
| Python 工程包 | requirements；实际 CadQuery 2.8.0、VTK 9.6.2、numpy 2.4.6、meshio 5.3.5 | [Linux 工程链](linux-cae-deployment.md)、[真实模型工程链](live-linux-engineering-acceptance.md) |
| OpenFOAM/Gmsh | `Dockerfile.cae`，固定 OpenFOAM v2406 与 Gmsh 包版本；`VIBEFLOW_OPENFOAM_BASHRC` | [串行/续算](linux-cae-deployment.md)、[MPI/UDF](linux-mpi-udf-deployment.md)、[正式 Gmsh 工作流](linux-gmsh-deployment.md) |
| Pandoc/TeX | 基础 API 含 Pandoc；`Dockerfile.tex` 增补字体、引擎与工具 | [报告工程模板](report-engineering-templates.md)、[三引擎/SyncTeX](linux-tex-deployment.md) |
| FloTHERM helper | 当前应用的原生编译处理器；编译器来自工程镜像 | [编译/加载/旧成果保留/取消](linux-flotherm-deployment.md)；完整 helper 接口与依赖扩展仍属 FF-I09 |
| CalculiX | `Dockerfile.calculix` 校验源码并编译 ccx，PATH `/usr/local/bin/ccx` | [实际结构数值及 Windows 兼容](linux-calculix-deployment.md) |
| SU2 | `Dockerfile.su2`，PATH `/usr/local/bin/SU2_CFD` | [翼型求解/续算及 Windows 兼容](linux-su2-deployment.md) |
| ParaView | `fetch_paraview.py` + `Dockerfile.paraview`，`/opt/paraview`，私有 libglapi 兼容库 | [双后端原始离线契约](FF-D15-completion-review.md)，含真实渲染、数值、52 万点并发、取消与重启 |
| Node/npm | `fetch_node.py` + `Dockerfile.node-cli`；Node 24.22.3、npm 11.19.1 | [实际模块/安装/子进程及 CLI](linux-node-cli-deployment.md) |
| Codex/OpenCode/Kimi | `infra/cli-toolchain` 固定版本和锁文件，`npm ci` | 三 CLI 真实 DeepSeek 回合；正式 Codex API 会话、存储和清理 |
| Claude | 用户明确停止推进 | 不安装新增集成、不发起模型验收；既有历史材料保留 |

本次额外环境观察保存在 `tmp/deployment-snapshot-20261118/environment-observation.json`。工程镜像可找到 `g++` 和 `make`，未发现 `cmake` 与 `ccache`；现有已验收 wmake 和原生编译流程不依赖后二者。未来确需它们的外部构建任务须安装并验证，不能把“工具路径存在”当作支持完整后端。

### 构建与部署选择

所有工具 Dockerfile 使用 `API_IMAGE` 指定基底。需要完整工程镜像时按 API→CAE→TeX→CalculiX→SU2→ParaView→Node/CLI 逐层构建，下一层的 `API_IMAGE` 指向上一层结果；先用两个 fetch 工具准备并校验大体积官方安装包。每层的版本、来源和实际构建日志在上述专用文档中。

各 `docker-compose.<tool>.yml` 是单项构建覆盖，不要把多个覆盖文件简单叠加就宣称包含所有工具：最终 image/build 项可能覆盖前面的值。完成完整镜像后，部署时让 API 与 arq-worker 使用同一个成品镜像，并核对两者 app 摘要、工具路径及启动环境。当前已运行的组合镜像与增量发布有实际验收，但尚未声称当前整条构建顺序在全空缓存的新机器上完成过一次统一验收。

基础镜像/apt/Python requirements 中尚有非完整锁定的传递依赖；恢复同一部署应使用记录的精确 image ID。仅固定几个顶层工具版本不能保证未来新构建字节相同。可审查源码包与可还原的镜像备份承担不同职责。

## Windows 与 WSL

Windows 开发入口和私有配置见 [部署说明](deployment.md)、[工具配置](toolchain-deployment.md)。普通 WSL 命令使用 nobody；安装步骤可由管理员执行，不改变普通用户策略。

独立 Linux Codex 位于 `.tools/codex/0.167.0-alpha.4.1/linux-x64/bin/codex`，不再依赖编辑器扩展是否附带 Linux 程序。WSL Node 与 Codex 的 PATH、真实 `--version` 和缺少 Node 时的检测行为见 [F54](FF-F54-completion-review.md)。正式会话仍使用受控私有状态目录；裸 nobody 命令默认 home 的警告不作为平台会话恢复结果。

Windows/WSL 的网格、求解、MPI/UDF、TeX、CalculiX、SU2、ParaView 对应实际结果在上表专用文档中。不同 OS 的可执行文件路径不能相互替代；Linux Docker 不执行 Windows PE 程序，也不要求宿主把 WSL 程序暴露为 root。

## 数据、权限、代理与日志

- API/worker 共用 PostgreSQL、原 provider key 和对应存储配置；当前 S3 部署保留各自私有缓存卷，全部都应进入联合恢复计划。
- 当前 SeaweedFS 验收配置显式使用 `VIBEFLOW_ALLOW_PARTIAL_S3=true`；这不取消处理器自己的条件写入检查，也不代表任意 S3 产品均兼容。相关维护与混合清单边界见 [存储运维](storage-version-operations.md)。
- Linux API/worker 的 `seccomp=unconfined` 是既有 namespace 执行所需部署配置；不把它当作完整多租户沙箱认证。原沙箱及预算任务继续验收。
- 本机代理曾发生 TLS 连接失败；当前 API/worker 的 NO_PROXY 包含 `api.deepseek.com`，真实调用已通过。其他环境按实际网络配置，不复制本机网卡地址。未关闭 TLS 验证，也未自动重试已提交模型 POST。
- 运维记录容器名称/image ID、migration revision、job/attempt/session/turn ID；查询容器日志和相应公开 API 证据。服务密钥、原 Compose 解析配置和用户备份只存私有目录。

联合恢复的具体执行命令、原密钥验证与失败处理见 [恢复手册](deployment-recovery-runbook.md)。实际物理多主机、其他 S3 后端、商业求解器、正式公网认证与最终生产发布仍由原台账继续承接。
