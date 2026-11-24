# FF-F66 内置技能支持范围

2026-11-24 后续，FreeCAD、JAX-AM、JAX-FEM、PyFR、LAMMPS、FreeFem++、Scilab、PETSc、FEniCSx、OpenFAST、OpenVSP 与飞行器概念设计的技能详情已增加实际验证过的安装和运行路径。其他工具链按子任务继续验证。 [41 项子任务与数值结果](FF-F64-skill-native-toolchains.md)

2026-11-24 核对 41 个内置技能。每个技能详情先给出支持范围。依赖自配工具的 24 项同时在技能简介标明环境要求。已验收的路径按可复核的运行时、输入、命令与成果写入技能。

| 技能 | 支持范围 | 交付证据 |
|---|---|---|
| [vibeflow-PPT](../server/skills/vibeflow-PPT/SKILL.md) | 算例面板和 ppt:build 已实测 PPTX 模板、原生图形和表格、图片重建及独立发布。PPTX 旁白视频使用另行配置的 Impress、Poppler、eSpeak NG 和 FFmpeg。 | [运行验收](ppt-delivery.md) |
| [vibeflow-abaqus](../server/skills/vibeflow-abaqus/SKILL.md) | 自配环境参考。执行前在选定运行时确认Abaqus 求解器和有效许可证，并以相应版本的手册和实际命令结果为准。 | 依赖所在环境实测 |
| [vibeflow-aircraft-design](../server/skills/vibeflow-aircraft-design/SKILL.md) | 自配环境参考。执行前在选定运行时确认OpenVSP、VSPAERO 和适用的 Python 环境，并以相应版本的手册和实际命令结果为准。 | 依赖所在环境实测 |
| [vibeflow-calculix](../server/skills/vibeflow-calculix/SKILL.md) | Windows 和 Linux 已实测托管静力、FRD/日志及版本化发布。使用算例「作业与日志 → 开源求解」选择 .inp，或调用 calculix:solve。高级物理与并行组合以已验收算例为限。 | [运行验收](FF-C08-solver-delivery.md) |
| [vibeflow-case-dict](../server/skills/vibeflow-case-dict/SKILL.md) | 通过算例 CaseDict 的读取、校验和保存入口维护参数。A–E 确认还需当前阶段的真实成果和门禁核验。 | [运行验收](workflow-execution-contract.md) |
| [vibeflow-code-aster](../server/skills/vibeflow-code-aster/SKILL.md) | 已实测 Windows API 经非特权 WSL 执行串行 run_aster，发布 MED/.mess 并从历史计算库续算。选择 .export 提交 code-aster:solve。 | [运行验收](open-solver-native-runtime.md) |
| [vibeflow-comsol](../server/skills/vibeflow-comsol/SKILL.md) | 自配环境参考。执行前在选定运行时确认COMSOL 求解器和有效许可证，并以相应版本的手册和实际命令结果为准。 | 依赖所在环境实测 |
| [vibeflow-dualsphysics](../server/skills/vibeflow-dualsphysics/SKILL.md) | 自配 5.4.355 CPU。非特权 WSL 已实测 GenCase、二维溃坝、检查点和 PartVTK。 | [实际溃坝粒子](FF-F64-skill-native-toolchains.md#dualsphysics) |
| [vibeflow-elmer](../server/skills/vibeflow-elmer/SKILL.md) | Windows 串行 ElmerSolver 已实测 .sif 与目录网格、VTU/.result 发布及续算。通过 elmer:solve 或开源求解作业提交。MPI 示例需独立运行环境。 | [运行验收](elmer-runtime.md) |
| [vibeflow-fds](../server/skills/vibeflow-fds/SKILL.md) | 自配环境参考。执行前在选定运行时确认FDS 以及独立查看所需的 Smokeview，并以相应版本的手册和实际命令结果为准。 | 依赖所在环境实测 |
| [vibeflow-fenics](../server/skills/vibeflow-fenics/SKILL.md) | 自配环境参考。执行前在选定运行时确认与脚本 API 匹配的 FEniCS 或 FEniCSx 环境，并以相应版本的手册和实际命令结果为准。 | 依赖所在环境实测 |
| [vibeflow-fluent](../server/skills/vibeflow-fluent/SKILL.md) | 自配环境参考。执行前在选定运行时确认Fluent 求解器和有效许可证，并以相应版本的手册和实际命令结果为准。 | 依赖所在环境实测 |
| [vibeflow-freecad](../server/skills/vibeflow-freecad/SKILL.md) | 自配环境参考。已实测 Windows 便携版 1.1.4 无头运行、布尔切孔、FCStd 与 STEP/STL 重读。 | [原生几何回执](FF-F64-skill-native-toolchains.md#freecad) |
| [vibeflow-freefem](../server/skills/vibeflow-freefem/SKILL.md) | 自配环境参考。执行前在选定运行时确认FreeFem++，并行任务另需相应 MPI 构建，并以相应版本的手册和实际命令结果为准。 | 依赖所在环境实测 |
| [vibeflow-geometry](../server/skills/vibeflow-geometry/SKILL.md) | 平台 Python 环境已实测 CadQuery 的 build_geometry(ctx)、浏览器预览、STEP/STL 和版本化导出。 | [运行验收](FF-B01-B02-geometry-script-contract.md) |
| [vibeflow-hypermesh](../server/skills/vibeflow-hypermesh/SKILL.md) | 自配环境参考。执行前在选定运行时确认HyperMesh 客户端、批处理工具和有效许可证，并以相应版本的手册和实际命令结果为准。 | 依赖所在环境实测 |
| [vibeflow-jax-am](../server/skills/vibeflow-jax-am/SKILL.md) | 自配环境参考。已实测非特权 WSL CPU 的 JAX-AM 0.0.3 移动面热源计算、无热源对照与 VTU 重读。 | [实际热过程](FF-F64-skill-native-toolchains.md#jax-am) |
| [vibeflow-jax-fem](../server/skills/vibeflow-jax-fem/SKILL.md) | 自配环境参考。执行前在选定运行时确认JAX-FEM、JAX 与对应 CPU/GPU 后端，并以相应版本的手册和实际命令结果为准。 | 依赖所在环境实测 |
| [vibeflow-jobs](../server/skills/vibeflow-jobs/SKILL.md) | 平台已实测注册作业与通用命令的日志、取消、重试。命令在所选运行时执行，平台通过所属进程范围核验停止。 | [运行验收](owned-agent-shell.md) |
| [vibeflow-lammps](../server/skills/vibeflow-lammps/SKILL.md) | 自配环境参考。执行前在选定运行时确认包含目标输入样式的 LAMMPS 构建，并以相应版本的手册和实际命令结果为准。 | 依赖所在环境实测 |
| [vibeflow-laserbeamfoam](../server/skills/vibeflow-laserbeamfoam/SKILL.md) | V3.1 与 OpenFOAM v2506 已在非特权 WSL 构建并串行运行官方 Plate2D，验证温度、速度、金属体积分数和射线时间序列。其他运行时需复核依赖和构建结果。 | [板材算例](FF-F64-skill-native-toolchains.md#laserbeamfoam) |
| [vibeflow-latex](../server/skills/vibeflow-latex/SKILL.md) | 注册编译在配有 TeX Live 的 WSL 或 Linux 执行。XeLaTeX、LuaLaTeX、pdfLaTeX、biber、PDF 和 SyncTeX 有实际产物验收。 | [运行验收](FF-E03-latex-recipes.md) |
| [vibeflow-mesh](../server/skills/vibeflow-mesh/SKILL.md) | blockMesh、Gmsh、snappyHexMesh 和 cfMesh 有实际网格、质量门禁及成果发布证据。选择与算例输入匹配的 mesh.source。 | [运行验收](FF-B17-mesh-toolchains.md) |
| [vibeflow-moose](../server/skills/vibeflow-moose/SKILL.md) | 已实测 Windows API 经非特权 WSL 运行 MOOSE 串行扩散与瞬态续算，发布 CSV、Exodus 和检查点。托管 moose:solve 使用单文件 .i 与受支持的输入段。 | [运行验收](open-solver-native-runtime.md) |
| [vibeflow-nektar](../server/skills/vibeflow-nektar/SKILL.md) | 自配 Nektar++ 5.10.0。非特权 WSL 已实测 NekMesh、ADRSolver 与 FieldConvert。 | [实际 Helmholtz 场](FF-F64-skill-native-toolchains.md#nektar) |
| [vibeflow-openfast](../server/skills/vibeflow-openfast/SKILL.md) | 自配环境参考。执行前在选定运行时确认OpenFAST 与算例所需模块，并以相应版本的手册和实际命令结果为准。 | 依赖所在环境实测 |
| [vibeflow-openfoam](../server/skills/vibeflow-openfoam/SKILL.md) | OpenFOAM v2406 的 WSL 与 Linux 已实测网格、原生求解、运行取消、续算和成果发布。托管工作流是主入口，注册 MPI 与手动命令分别使用各自的输入。 | [运行验收](FF-C07-openfoam-matrix.md) |
| [vibeflow-openlb](../server/skills/vibeflow-openlb/SKILL.md) | 自配 OpenLB。非特权 WSL GCC 13 构建官方二维 Poiseuille，已读回原生 VTK。 | [实际 Poiseuille 场](FF-F64-skill-native-toolchains.md#openlb) |
| [vibeflow-openradioss](../server/skills/vibeflow-openradioss/SKILL.md) | 原 OpenRadioss `latest-20260728` 与 OpenCourant `latest-20261011` 均在非特权 WSL 完成单砖拉伸的 Starter、Engine 和 VTK 转换。 | [原版与延续版实算](FF-F64-skill-native-toolchains.md#openradioss-与-opencourant) |
| [vibeflow-openvsp](../server/skills/vibeflow-openvsp/SKILL.md) | 自配环境参考。执行前在选定运行时确认OpenVSP Python API、VSPAERO 及其依赖，并以相应版本的手册和实际命令结果为准。 | 依赖所在环境实测 |
| [vibeflow-petsc](../server/skills/vibeflow-petsc/SKILL.md) | 自配环境参考。执行前在选定运行时确认匹配项目的 PETSc、编译器与必要的 MPI/求解库，并以相应版本的手册和实际命令结果为准。 | 依赖所在环境实测 |
| [vibeflow-postprocess](../server/skills/vibeflow-postprocess/SKILL.md) | 已实测从已确认求解版本提取 CSV、PNG 和证据摘要。工作流确认 D 消费实际发布的同版本结果。 | [运行验收](FF-D24-postprocess-evidence.md) |
| [vibeflow-pyfr](../server/skills/vibeflow-pyfr/SKILL.md) | 自配环境参考。执行前在选定运行时确认PyFR 与所选后端及匹配的 MPI 工具链，并以相应版本的手册和实际命令结果为准。 | 依赖所在环境实测 |
| [vibeflow-report](../server/skills/vibeflow-report/SKILL.md) | Pandoc HTML/DOCX 已实测，构建依赖所选运行时的 Pandoc。报告作业的证据与输出版本独立保留。 | [运行验收](report-workflows.md) |
| [vibeflow-scilab](../server/skills/vibeflow-scilab/SKILL.md) | 自配环境参考。执行前在选定运行时确认与脚本匹配的 Scilab 批处理环境，并以相应版本的手册和实际命令结果为准。 | 依赖所在环境实测 |
| [vibeflow-skill-creator](../server/skills/vibeflow-skill-creator/SKILL.md) | 技能详情、私有及组织技能包已实测。通过技能管理接口保存或安装，使用 Agent 草稿联想和 get_skill 读取。 | [运行验收](FF-F63-original-skill-catalog.md) |
| [vibeflow-sphinxsys](../server/skills/vibeflow-sphinxsys/SKILL.md) | 非特权 WSL 已核验固定版本的二维 CPU 溃坝，VTP、压力和能量均有原生回执。另有运行时依赖需随部署环境复核。 | [原生算例](FF-F64-skill-native-toolchains.md#sphinxsys) |
| [vibeflow-starccm](../server/skills/vibeflow-starccm/SKILL.md) | 自配环境参考。执行前在选定运行时确认STAR-CCM+ 批处理工具和有效许可证，并以相应版本的手册和实际命令结果为准。 | 依赖所在环境实测 |
| [vibeflow-su2](../server/skills/vibeflow-su2/SKILL.md) | Windows 和 Linux 已实测 SU2_CFD、历史曲线、流场与续算。托管 su2:solve 提交 .cfg 与算例内网格。MPI 示例依赖匹配的构建和环境。 | [运行验收](su2-publication.md) |
| [vibeflow-toolchain](../server/skills/vibeflow-toolchain/SKILL.md) | 已实测 Windows、非特权 WSL 和 Linux 的工具探测及作业。以当前部署的 /api/v1/runtime/capabilities 和所选作业环境中的探测为准。 | [运行验收](owned-runtime-probes.md) |
| [vibeflow-viz](../server/skills/vibeflow-viz/SKILL.md) | 实际 VTK/trame 会话已验证算例内场景脚本、截图和原生 VTK、XDMF、Exodus 读取。使用受信任的算例脚本，并以当前会话的日志核验执行。 | [运行验收](FF-D25-result-readers.md) |

完整工具链安装、未验证的求解组合和领域基准继续由 FF-F64 跟踪。各部署按本机能力探测确认可用程序。

隔离 API 逐项读取 41 份技能，验证 24 份自配环境提示、私有覆盖、匿名与未知技能拒绝。本机实际能力探测确认 CalculiX、Code_Aster、Elmer、MOOSE、SU2、Pandoc、ParaView，以及 WSL OpenFOAM、Gmsh、latexmk 和服务端 CadQuery/VTK 等依赖。API 已退出。本轮模型调用 0 次。 [本机实测](../../tmp/ff-f66-skill-capabilities-cpfh0jgy/skill-capabilities.json) · [复验入口](../server/skill_capabilities_acceptance.py)
