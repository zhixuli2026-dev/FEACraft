# Gmsh 正式网格工作流与 Linux 部署

2026-11-18。此前平台可通过手动命令运行 Gmsh 并预览 MSH，但正式 `mesh.generate` 仅接受 blockMesh。现在同一个注册处理器支持 `mesh.source=gmsh`，实际执行 Gmsh → gmshToFoam → checkMesh，并沿用已封存输入、质量门槛、不可变发布、工程确认、求解、后处理和报告流程。

## 使用方式

在 caseDict 的 `mesh` 块设置 `source gmsh;`，在运行目录的 `system/mesh.geo` 编写三维几何及网格设置，并提供原有 `controlDict`、物理字典和初始场。Gmsh 分支不要求 blockMeshDict。物理曲面名称应与初始场中的边界名称一致，物理体用于保留体网格。

处理器使用固定参数 `-3 -format msh2`，将生成文件写到运行目录的 `gmsh.msh`；转换和质量检查使用同一配置的 OpenFOAM 环境。发布包括 MSH、gmsh.log、gmshToFoam.log、checkMesh.log、polyMesh、质量/边界报告和实际可执行文件哈希。`/mesh/results` 的工具链记录包含 `mesh_source`，旧 blockMesh 发布按原默认解释。

普通 blockMesh 分支继续读取 `system/blockMeshDict`。两个来源共用实际 checkMesh 门槛；失败不能发布为合格网格。网格生成原始输入保持不变，后续求解依赖的是正式网格发布。

示例 [mesh.geo](../server/examples/gmsh-conduction/mesh.geo) 使用单位立方体、64 个六面体单元及 xMin/xMax/insulated 物理边界，设置方式参考 [Gmsh 官方手册](https://gmsh.info/doc/texinfo/)。它可配合既有热传导字典验证解析解 `T(x)=300+100*x`。

## 部署

构建与启动沿用 [Linux 工程部署](linux-cae-deployment.md)。`Dockerfile.cae` 现在同时安装 OpenFOAM、strace 和 Debian 软件源中的 `gmsh=4.13.1+ds1-6`。新构建参数 `GMSH_VERSION` 同时暴露在 API/worker 的 Compose 覆盖文件中；默认和自定义参数均经过实际渲染检查。更换工具版本后应重新验证目标工程。

当前 S3 验收部署（57700）的 API 与 worker 使用 `vibeflow/api:linux-gmsh-v1-20261118`，两容器各 466 个 app Python 文件与工作区一致。FS 部署（62883）仍为工程报告模板镜像。镜像安装来源与构建日志在 `tmp/linux-gmsh-20261118/build-v1.log`，未改动数据卷。

## 实际验收与边界

- Windows/WSL 与 Linux/S3 均完成正式 CadQuery → Gmsh/gmshToFoam/checkMesh → laplacianFoam → 后处理 → HTML/DOCX 流程。64 单元温度场从实际坐标与输出文件独立计算，解析解 L2 和最大误差均为 0 K；MSH 的单元类型、物理分组与预览 VTU 点/单元数一致，报告按文件哈希下载核验。
- Linux blockMesh 复验使用已验收的 10×4×2 网格，共 80 单元，通过同一完整流程及解析解检查。首次误用旧 20×2×2 网格，实际 checkMesh 报告 72 个单元的 determinant 低于其门槛，作业拒绝发布；失败原件保留，未降低质量要求。
- 修改实际 `mesh.geo` 的网格密度后，网格、求解和报告三类结果均标记过期；恢复完全相同的原输入后重新有效，历史报告下载哈希保持。
- 真实 DeepSeek `deepseek-flash` 经平台对话选择 `read_text_file`，读取实际生成的 `B-mesh/toolchain.json`，返回文件内的执行编号、网格来源和 Gmsh 路径。执行编号未写入提示词；工具结果和最终回答持久化核验通过。记录为 1 次平台模型请求、平台 HTTP 200；未对上游 HTTP 请求数单独计量，不将其表述为仅一次上游调用。模拟 LLM 0。

三条完整工程流程共 18 个成功原生作业，停止凭据全部核验。工程执行、源文件过期规则与 Compose 配置检查、真实模型读取分别记账；模型读取不代表模型自主完成了整条工程创建流程。

证据目录：

| 范围 | 位置 |
| --- | --- |
| Windows/WSL Gmsh | `tmp/linux-gmsh-20261118/windows-gmsh-6656a432a9/result.json` |
| Linux Gmsh | `tmp/linux-gmsh-20261118/linux-gmsh-7686a43294/result.json` |
| Linux blockMesh | `tmp/linux-gmsh-20261118/linux-blockMesh-c858342117/result.json` |
| 旧网格失败 | `tmp/linux-gmsh-20261118/linux-blockMesh-90358e4a91/result.json` |
| 输入变更/恢复、实际部署和 Compose | `tmp/linux-gmsh-20261118/deployment-result.json` |
| 真实模型读取 | `tmp/linux-gmsh-20261118/linux-gmsh-7686a43294/model-readback.json`，同目录保留事件及历史 |

汇总见 `tmp/mainline-linux-gmsh-evidence/validation.json`。生产实现从工程审查 v1 起未再改变；v2 只修正 blockMesh 验收网格，v3 加入输入变更与部署检查，真实模型入口另行审查与封存。

本次补齐正式三维 Gmsh 来源；边界层、自适应、网格无关性及其他来源仍属于 FF-B15，全部产物映射仍按 FF-B18 推进。未声称任意外部 Include/文件或工具升级具有完整依赖闭包。TeX/SyncTeX、其他外部工具和完整生产部署条件继续推进。

主线 **49/53，剩 4 项**；资源 **11/17，剩 6 块**；全功能 **2/295，剩 293 项**。N7-05 保持进行中，原范围不变。
