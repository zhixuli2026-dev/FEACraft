# FF-F64 内置技能与原生工具链验证

从实际发布的 41 个内置技能生成子任务。表中的验收适用于链接所列版本、运行时和算例。各部署安装相应工具后复核命令路径和许可。新增算例按 T08 核对实际输入、日志、数值和产物。 [技能目录](FF-F63-original-skill-catalog.md) · [交付计划](full-function-delivery-plan.md)

2026-11-25，36/41 个技能具备当前范围的实际工具或平台作业证据。余下 5 个需要 Abaqus、COMSOL、Fluent、HyperMesh、STAR-CCM+ 的可用厂商安装、许可证和算例，FF-F64 母项继续验收。

| 子任务 | 技能 | 当前本机证据 | 验收记录 |
|---|---|---|---|
| FF-F64.01 | [vibeflow-PPT](../server/skills/vibeflow-PPT/SKILL.md) | 已有实际路径。支持版本和适用范围见验收 | [运行验收](ppt-delivery.md) |
| FF-F64.02 | [vibeflow-abaqus](../server/skills/vibeflow-abaqus/SKILL.md) | 待对应厂商安装、许可证和实际案例 | [支持范围](FF-F66-skill-capability-statements.md) |
| FF-F64.03 | [vibeflow-aircraft-design](../server/skills/vibeflow-aircraft-design/SKILL.md) | 实际矩形机翼几何、重读、三点迎角扫描 | [实际机翼算例](#openvsp-与-vspaero) |
| FF-F64.04 | [vibeflow-calculix](../server/skills/vibeflow-calculix/SKILL.md) | 已有实际路径。支持版本和适用范围见验收 | [运行验收](FF-C08-solver-delivery.md) |
| FF-F64.05 | [vibeflow-case-dict](../server/skills/vibeflow-case-dict/SKILL.md) | 已有实际路径。支持版本和适用范围见验收 | [运行验收](workflow-execution-contract.md) |
| FF-F64.06 | [vibeflow-code-aster](../server/skills/vibeflow-code-aster/SKILL.md) | 已有实际路径。支持版本和适用范围见验收 | [运行验收](open-solver-native-runtime.md) |
| FF-F64.07 | [vibeflow-comsol](../server/skills/vibeflow-comsol/SKILL.md) | 待对应厂商安装、许可证和实际案例 | [支持范围](FF-F66-skill-capability-statements.md) |
| FF-F64.08 | [vibeflow-dualsphysics](../server/skills/vibeflow-dualsphysics/SKILL.md) | 实际非特权 WSL。GenCase、二维溃坝、检查点、BI4 与流体 VTK | [溃坝实算](#dualsphysics) |
| FF-F64.09 | [vibeflow-elmer](../server/skills/vibeflow-elmer/SKILL.md) | 已有实际路径。支持版本和适用范围见验收 | [运行验收](elmer-runtime.md) |
| FF-F64.10 | [vibeflow-fds](../server/skills/vibeflow-fds/SKILL.md) | 实际非特权 WSL。官方固体传热基准、六测点与解析解 | [FDS 算例](#fds) |
| FF-F64.11 | [vibeflow-fenics](../server/skills/vibeflow-fenics/SKILL.md) | 实际 WSL 作业。DOLFINx 两级网格和泊松误差收敛 | [DOLFINx 算例](#fenicsx) |
| FF-F64.12 | [vibeflow-fluent](../server/skills/vibeflow-fluent/SKILL.md) | 待对应厂商安装、许可证和实际案例 | [支持范围](FF-F66-skill-capability-statements.md) |
| FF-F64.13 | [vibeflow-freecad](../server/skills/vibeflow-freecad/SKILL.md) | 实际 Windows 无头 FreeCAD。四孔板及 FCStd、STEP、STL 重读 | [几何算例](#freecad) |
| FF-F64.14 | [vibeflow-freefem](../server/skills/vibeflow-freefem/SKILL.md) | 实际 WSL 作业。P2 三角单元泊松方程与解析解对照 | [网格与数值](#本机工具包实测) |
| FF-F64.15 | [vibeflow-geometry](../server/skills/vibeflow-geometry/SKILL.md) | 已有实际路径。支持版本和适用范围见验收 | [运行验收](FF-B01-B02-geometry-script-contract.md) |
| FF-F64.16 | [vibeflow-hypermesh](../server/skills/vibeflow-hypermesh/SKILL.md) | 待对应厂商安装、许可证和实际案例 | [支持范围](FF-F66-skill-capability-statements.md) |
| FF-F64.17 | [vibeflow-jax-am](../server/skills/vibeflow-jax-am/SKILL.md) | 实际 WSL CPU。移动面热源、无热源对照及原生 VTU | [JAX-AM 算例](#jax-am) |
| FF-F64.18 | [vibeflow-jax-fem](../server/skills/vibeflow-jax-fem/SKILL.md) | 实际 WSL CPU。泊松场、伴随梯度、中心差分和 VTU | [JAX-FEM 算例](#jax-fem) |
| FF-F64.19 | [vibeflow-jobs](../server/skills/vibeflow-jobs/SKILL.md) | 已有实际路径。支持版本和适用范围见验收 | [运行验收](owned-agent-shell.md) |
| FF-F64.20 | [vibeflow-lammps](../server/skills/vibeflow-lammps/SKILL.md) | 实际 WSL 作业。双粒子谐振键能量极小化与轨迹读回 | [输入与轨迹](#本机工具包实测) |
| FF-F64.21 | [vibeflow-laserbeamfoam](../server/skills/vibeflow-laserbeamfoam/SKILL.md) | 实际非特权 WSL。V3.1 与 OpenFOAM v2506，官方 Plate2D，原生场和射线文件 | [板材算例](#laserbeamfoam) |
| FF-F64.22 | [vibeflow-latex](../server/skills/vibeflow-latex/SKILL.md) | 已有实际路径。支持版本和适用范围见验收 | [运行验收](FF-E03-latex-recipes.md) |
| FF-F64.23 | [vibeflow-mesh](../server/skills/vibeflow-mesh/SKILL.md) | 已有实际路径。支持版本和适用范围见验收 | [运行验收](FF-B17-mesh-toolchains.md) |
| FF-F64.24 | [vibeflow-moose](../server/skills/vibeflow-moose/SKILL.md) | 已有实际路径。支持版本和适用范围见验收 | [运行验收](open-solver-native-runtime.md) |
| FF-F64.25 | [vibeflow-nektar](../server/skills/vibeflow-nektar/SKILL.md) | 实际非特权 WSL。Gmsh 导入、Helmholtz 谱阶收敛和 `.fld`、VTU | [Nektar++ 算例](#nektar) |
| FF-F64.26 | [vibeflow-openfast](../server/skills/vibeflow-openfast/SKILL.md) | 实际 Windows AOC 风机耦合求解，27 个频道对比官方数据 | [风机算例](#openfast) |
| FF-F64.27 | [vibeflow-openfoam](../server/skills/vibeflow-openfoam/SKILL.md) | 已有实际路径。支持版本和适用范围见验收 | [运行验收](FF-C07-openfoam-matrix.md) |
| FF-F64.28 | [vibeflow-openlb](../server/skills/vibeflow-openlb/SKILL.md) | 实际非特权 WSL。官方二维 Poiseuille 收敛、14 帧原生 VTK、中心线曲线 | [OpenLB 算例](#openlb) |
| FF-F64.29 | [vibeflow-openradioss](../server/skills/vibeflow-openradioss/SKILL.md) | 原 `latest-20260728` 发行包已核验。非特权 WSL 的 Starter、Engine、拉伸原件和 VTK | [原版与延续版实算](#openradioss-与-opencourant) |
| FF-F64.30 | [vibeflow-openvsp](../server/skills/vibeflow-openvsp/SKILL.md) | Windows 平台作业。OpenVSP Python API、VSPAERO 原生进程及成果 | [实际机翼算例](#openvsp-与-vspaero) |
| FF-F64.31 | [vibeflow-petsc](../server/skills/vibeflow-petsc/SKILL.md) | 实际 WSL 作业。PETSc 3.19.6 的 CG/Jacobi 求解 | [线性系统与残差](#本机工具包实测) |
| FF-F64.32 | [vibeflow-postprocess](../server/skills/vibeflow-postprocess/SKILL.md) | 已有实际路径。支持版本和适用范围见验收 | [运行验收](FF-D24-postprocess-evidence.md) |
| FF-F64.33 | [vibeflow-pyfr](../server/skills/vibeflow-pyfr/SKILL.md) | 实际 WSL OpenMP。二维三阶 Euler 涡旋导入、求解、VTU 与解析对照 | [PyFR 算例](#pyfr) |
| FF-F64.34 | [vibeflow-report](../server/skills/vibeflow-report/SKILL.md) | 已有实际路径。支持版本和适用范围见验收 | [运行验收](report-workflows.md) |
| FF-F64.35 | [vibeflow-scilab](../server/skills/vibeflow-scilab/SKILL.md) | 实际 WSL 无头作业。指数衰减微分方程 41 点 | [ODE 与误差](#本机工具包实测) |
| FF-F64.36 | [vibeflow-skill-creator](../server/skills/vibeflow-skill-creator/SKILL.md) | 已有实际路径。支持版本和适用范围见验收 | [运行验收](FF-F63-original-skill-catalog.md) |
| FF-F64.37 | [vibeflow-sphinxsys](../server/skills/vibeflow-sphinxsys/SKILL.md) | 实际非特权 WSL。官方 2D 溃坝、VTP 粒子、压力和能量历史 | [原生算例](#sphinxsys) |
| FF-F64.38 | [vibeflow-starccm](../server/skills/vibeflow-starccm/SKILL.md) | 待对应厂商安装、许可证和实际案例 | [支持范围](FF-F66-skill-capability-statements.md) |
| FF-F64.39 | [vibeflow-su2](../server/skills/vibeflow-su2/SKILL.md) | 已有实际路径。支持版本和适用范围见验收 | [运行验收](su2-publication.md) |
| FF-F64.40 | [vibeflow-toolchain](../server/skills/vibeflow-toolchain/SKILL.md) | 已有实际路径。支持版本和适用范围见验收 | [运行验收](owned-runtime-probes.md) |
| FF-F64.41 | [vibeflow-viz](../server/skills/vibeflow-viz/SKILL.md) | 已有实际路径。支持版本和适用范围见验收 | [运行验收](FF-D25-result-readers.md) |

## 本机工具包实测

`vibeflow-linux` 使用 Ubuntu 24.04 的发行版包。本轮通过正式 `POST /api/v1/cases/{id}/jobs` 提交，每份输入单独封存。实际执行身份为 UID 65534。校验原输入、作业成功、退出范围为空，以及每个成果下载的原始字节大小和摘要。四项模型调用均为 0。 [完整作业回执](../../tmp/ff-f64-managed-tools-jw19txik/skill-native-toolchain.json) · [定向回归](../server/skill_native_toolchain_acceptance.py)

| 工具 | 安装和实际输入 | 可独立回算的结果 |
|---|---|---|
| LAMMPS 7 Feb 2024 | `lammps` 配合 `lammps-data`。以 `lmp -in relax-dimer.in` 最小化真实双粒子键能。 [样例](../server/examples/lammps/relax-dimer.in) · [官方命令](https://docs.lammps.org/minimize.html) | 键长从 1.4 收敛到 1.0，无量纲。输出原始轨迹和 LAMMPS 日志。 |
| FreeFem++ 4.13 | `freefem++`。以 `FreeFem++ -nw poisson.edp` 求解单位正方形上的齐次 Dirichlet 泊松方程。 [样例](../server/examples/freefem/poisson.edp) · [公开文档](https://doc.freefem.org/tutorials/poisson.html) | 2,048 个三角单元。L² 误差为 `8.6005e-6`。33 个剖面点最大误差为 `1.2733e-6`。输出网格、CSV 和剖面。 |
| Scilab 2024.0.0 | `scilab-cli`、`dpkg-dev`。以 `env HOME="$PWD" scilab-cli -nb -f decay.sce` 在工作区执行。 [样例](../server/examples/scilab/decay.sce) · [官方 ODE API](https://help.scilab.org/docs/2024.1.0/en_US/ode.html) | 在 0 到 2 秒对照 `exp(-2t)`，41 个点的最大绝对误差为 `1.2193e-10`，末值为 0.01831563887。 |
| PETSc 3.19.6 | `python3-petsc4py`、`python3-petsc4py-real`，将 `PETSC_DIR` 指向当前实数构建，再使用发行版 Python 启动。 [样例](../server/examples/petsc/poisson_line.py) · [官方 KSP API](https://petsc.org/release/petsc4py/reference/petsc4py.PETSc.KSP.html) | 63 个内部节点。CG/Jacobi 在 32 次迭代后收敛，矩阵回算残差为 0。CSV 可与 `u(x)=x(1−x)` 逐点对照。 |

Scilab 首次托管运行因发行版启动脚本找不到 `dpkg-architecture` 无法加载库。补 `dpkg-dev` 并使用工作区内的 HOME 后复跑通过。PETSc 的 CPU 实数包和 Python 绑定入口分别安装，选择对应 `PETSC_DIR` 后复跑通过。Docker 占用和 Windows C 盘在安装前后分别采样。Scilab 依赖更新了 WSL 的 `libpcre2-8-0` 补丁版本，安装记录留在 D 盘，没有请求服务重启。 [Scilab 安装](../../tmp/ff-f64-native-scilab-20261124) · [PETSc 安装](../../tmp/ff-f64-native-petsc-20261124)

## JAX-AM

在非特权 WSL CPU 环境核验官方 JAX-AM `0.0.3`，源码提交 `b4ee1363cde4ce98a1532c5d255015014a6f74d7`，JAX 0.8.2。复用原 JAX/FEniCSx Python，在独立目录补齐 pandas、matplotlib 和 PyYAML。两个原生 `AM_3d` 算例各 640 个六面体、16 步，跑至 80 μs。无热源场最大值保持 300 K，20 W 移动面热源的峰值由 300 K 升至 816.49 K 和 1,165.96 K。meshio 重读 6 个原生 VTU，末帧温度逐单元与计算值转为 float32 后完全相同。本轮温度低于材料固相线，验收的是移动热源和固态热传导。模型调用为 0。[样例](../server/examples/jax-am/surface_heating.py) · [数值回执](../../tmp/ff-f64-jax-am-20261124/jax-am-result/heat.json) · [6 份 VTU 摘要](../../tmp/ff-f64-jax-am-20261124/vtk-verification.json) · [上游算例](https://github.com/CMSL-HKUST/jax-am/tree/b4ee1363cde4ce98a1532c5d255015014a6f74d7/applications/cfd/simple)

## OpenFAST

Windows 的 OpenFAST 4.2.1 单线程 x64 官方二进制经发布摘要核对。官方 AOC_WSt 案例使用该发布引用的 r-test commit `6fda1b18fe6d720fdd47c6b18823b3a755cd004c`。输入在独立目录执行并生成新的 `.outb`。ElastoDyn、InflowWind、AeroDyn、ServoDyn 耦合运行至 35 秒。读取 5 到 35 秒的 601 个样本和 27 个频道，与原参考结果比较。按各参考频道峰值和 1 的较大值归一化，最大误差为 `7.005e-4`，转速频道误差为 `2.336e-5`。模型调用为 0。 [原生结果](../../tmp/ff-f64-openfast-20261124/aoc-result.json) · [官方发布](https://github.com/OpenFAST/openfast/releases/tag/v4.2.1) · [参考案例](https://github.com/OpenFAST/r-test/tree/6fda1b18fe6d720fdd47c6b18823b3a755cd004c/glue-codes/openfast/AOC_WSt)

## OpenVSP 与 VSPAERO

实际安装的 Windows x64 OpenVSP 3.57.1 使用 Python 3.11 接口，发行包 SHA-256 为 `d8ad0e75d0ab5132ae4b2a81dedcb6e28704280f15809b6396f02de8ae54eefe`。正式算例作业封存样例脚本，重开生成的对称矩形翼，再调用原生 VSPAERO。迎角 0°、3°、6° 的升力系数依次为 0、0.265357908419、0.529694043835。核对正迎角升力增长及零迎角对称性，下载 `.vsp3`、`.polar`、`.adb`、CSV 与 JSON 原件并核对摘要，已验证进程退出。模型调用为 0。 [作业回执](../../tmp/ff-f64-managed-tools-44z0b30j/skill-native-toolchain.json) · [样例](../server/examples/openvsp/wing_polar.py) · [官方发行包](https://openvsp.org/download.php)

## FEniCSx

在现有 WSL 发行版安装独立 Conda 环境，选取 Python 3.12、DOLFINx 0.11.0、MPICH 和配套的 PETSc 3.25.6。实际非特权作业以 Lagrange P1 离散单位方形上的泊松方程，粗细网格分别为 1,152 与 4,608 个三角单元。L² 误差由 `1.6291e-4` 降至 `4.0793e-5`，误差比为 3.99358。细网格的 XDMF 声明 2,401 个节点标量，HDF5 原件和 XDMF 均经平台原件下载核对，进程退出已验证。模型调用为 0。 [平台回执](../../tmp/ff-f64-managed-tools-cates9dj/skill-native-toolchain.json) · [样例](../server/examples/fenicsx/poisson_square.py) · [官方安装](https://fenicsproject.org/download/) · [DOLFINx API](https://docs.fenicsproject.org/dolfinx/v0.11.0/python/demos/demo_poisson.html)

## PyFR

PyFR 3.1 用 Python 3.12、MPICH 5.0.1 与官方要求的 LIBXSMM 提交 `5424ec5e122172ec263ef0cc6473d44b8be16fb2`。非特权 WSL 使用 GCC 13.3、OpenMP 单线程。官方 `2d-euler-vortex` 以原 Gmsh 网格和三阶配置运行，终止时间设为 0.1，输出间隔设为 0.05，时间步仍为 0.005。三个实际 `.pyfrs` 解和 VTU 原件均记录摘要。 [官方安装](https://pyfr.readthedocs.io/en/latest/installation.html) · [输入来源](https://github.com/PyFR/PyFR-Test-Cases/tree/10e42880081b65b4a11736ffccb385a0b50bfe79/2d-euler-vortex)

VTK 原生 XML 读取器从导出文件读回 400 个单元、6,400 个点。与平移至 `t=0.1` 的解析场比较，密度、压力与速度的相对 L² 误差分别为 `5.27e-5`、`6.05e-5` 和 `5.06e-4`。模型调用为 0。 [数值与文件回执](../../tmp/ff-f64-pyfr-20261124/euler-vortex-result.json)

## JAX-FEM

JAX-FEM 0.0.12 使用非特权 WSL CPU、JAX 0.8.2、Python 3.12。实际 Q1 泊松算例有 144 个四边形和 169 个节点，解与制造解的最大绝对误差为 `3.42955e-4`。体源缩放的伴随导数为 `0.02347825704997`，中心差分为 `0.02347825704978`。读取原始 float32 VTU 时，回读误差与求解阶段结果的差值在写出精度范围内。模型调用为 0。 [数值与 VTU 核对](../../tmp/ff-f64-jax-fem-20261124/numerical-verification.json) · [样例](../server/examples/jax-fem/poisson_sensitivity.py) · [安装说明](https://github.com/deepmodeling/jax-fem/blob/main/docs/source/guide/Installation.md)

## FreeCAD

官方 FreeCAD 1.1.4 Windows x64 便携包大小为 418,088,341 B，SHA-256 为 `4828741fc91ee37fafcdb97a1abacb18b04ba451ac4372d9ff7a7349b36f4d6d`。下载断线后从确认的 HTTP 206 位置续传，校验后在 D 盘解压。Windows 的 `tar.exe` 缺少该包所用的 LZMA 编解码器，改用非特权 WSL 私有目录中的 Ubuntu 7-Zip 解压。FreeCAD 在独立配置和临时目录以真实无头进程运行。

实际构造 30×20×4 mm 的四孔板并核对一个有效实体，重开 FCStd 并重读 STEP 和 STL。FCStd 与 STEP 的体积均为 2,198.938070 mm³，与解析值一致。STL 实际为 1,372 面的闭合网格，体积相对误差约 0.00851%。已记录四份输出的摘要，进程正常退出，模型调用为 0。[几何脚本](../server/examples/freecad/drilled_plate.py) · [数值](../../tmp/ff-f64-freecad-20261124/native-plate/geometry/plate.json) · [原件摘要](../../tmp/ff-f64-freecad-20261124/native-plate/artifacts.json) · [官方版本](https://github.com/FreeCAD/FreeCAD/releases/tag/1.1.4)

## DualSPHysics

官方课程站点的大文件传输超时。复用 GitHub 上游固定提交 `ef3721a861fda961f0e2f9ec4cd317b19de99086` 的 CPU 源码与自带 GenCase 和 PartVTK。GCC 13.3 增加 `-include cstdint`。大计算单元 `JSphCpu.cpp` 在 `-O3` 编译期限内未结束，该单元改用 `-O1` 后链接通过，其余单元用 `-O3`。本算例采用不启用 Chrono、WaveGen 和 MoorDynPlus 的 CPU 构建，运行横幅 5.4.355。 [上游算例](https://github.com/DualSPHysics/DualSPHysics/tree/ef3721a861fda961f0e2f9ec4cd317b19de99086/examples/main/01_DamBreak) · [构建回执](../../tmp/ff-f64-dualsphysics-20261124/build-source.json) · [输入来源](../../tmp/ff-f64-dualsphysics-20261124/input-source.json)

GenCase 使用官方二维溃坝定义生成 20,000 个流体粒子及 1,001 个边界粒子。初次两线程试验在执行期限内保存到 0.52 秒。从完整第 51 帧的 0.510025 秒使用原输入续跑，四线程到第 200 帧的 2.000004 秒，实际退出 0。独立 PartVTK 导出并按粒子 ID 对照重启前后，20,000 个流体粒子的位置差为 0。最终仍有 19,471 个流体粒子，日志记载排除 529 个。流体最远 x 由 1.0 到约 4.0 m，最终速度、密度和压力有限。记录原生 BI4 端点和 VTK 哈希，核验求解器、前处理和转换进程均已退出，模型调用 0 次。 [原件与回执](../../tmp/ff-f64-dualsphysics-20261124/numerical-result.json) · [续算日志](../../tmp/ff-f64-dualsphysics-20261124/native-results/resume.log) · [回读工具](../server/examples/dualsphysics/verify_dam_break.py)

## OpenLB

对照官方发行方的 Zenodo 记录验证 `olb-1.9r0.tgz` 的大小和 MD5，并保留 SHA-256。解包后用 GCC 13.3 和发行包自带依赖，在非特权 WSL 串行构建官方 `examples/laminar/poiseuille2d`。归档标注 1.9.0，随包 `rules.mk` 的版本常量及实际运行横幅显示 1.8r1，两处原始值均已记录。 [官方发行](https://www.openlb.net/download/) · [Zenodo 1.9.0](https://zenodo.org/records/17899765) · [下载回执](../../tmp/ff-f64-openlb-20261124/download.json)

算例在 46,600 步、18.64 秒物理时间收敛。求解器速度相对 L2 为 0.00114529。14 个 `.pvd` 时间条目的 `.vtm`、`.vti` 全部存在并记录摘要。最终 5,459 点，中心线 51 点与解析抛物线比较，最大轴向误差 0.000988 m/s、最大横向速度 0.00000223 m/s。在无 gnuplot 的本机仍生成原生 VTK。退出 0，核验进程已结束。模型调用 0 次。 [求解日志](../../tmp/ff-f64-openlb-20261124/solver.log) · [数值回执](../../tmp/ff-f64-openlb-20261124/numerical-result.json) · [原生序列](../../tmp/ff-f64-openlb-20261124/native-vtk/poiseuille2d.pvd) · [回读工具](../server/examples/openlb/verify_poiseuille.py)

## Nektar++

用 Nektar++ 官方 Ubuntu 24.04 软件源的签名和 SHA-256 核对 5.10.0 的四个软件包，解包到非特权 WSL 私有目录。实际 NekMesh 导入官方教程网格，ADRSolver 完成三种展开阶数，FieldConvert 以网格和条件文件把原生 `.fld` 转为 VTU。保留 5 模态的原始教程条件，并以同网格比较 3、5、7 模态。VTK 重读每个网格的 `u`，与解析场 `cos(πx) cos(πy)` 逐点核对。三个最大误差分别为 2.49×10⁻²、3.52×10⁻⁴、3.02×10⁻⁶。7 模态 4,214 点、3,096 个单元，求解器 L2 为 8.99×10⁻⁷。原件摘要保留，三类原生进程均已退出，模型调用 0 次。本轮证据覆盖串行二维 Helmholtz。 [官方教程](https://doc.nektar.info/tutorials/5.10.0/basics/helmholtz/basics-helmholtz.html) · [下载与摘要](../../tmp/ff-f64-nektar-20261124/downloads.json) · [数值回执](../../tmp/ff-f64-nektar-20261124/numerical-result.json) · [回读工具](../server/examples/nektar/verify_helmholtz.py)

## LaserbeamFoam

OpenFOAM v2506 由 Ubuntu 24.04 官方软件源的五个已验签包解出。LaserbeamFoam V3.1 发布源码在独立 WSL 目录以 UID 65534 完成 `Allwmake -j 3`。原样运行官方 `Plate2D`，先 `blockMesh` 和 `setFields`，随后完成 6,594 步到 0.01 秒物理时间。按 `foamToVTK` 的时间索引回读两个时刻的原生数据，均为 3,200 单元、6,642 点。初始单元温度 300 K，末态最高 11,890.78 K、最大速度 4.44958 m/s，金属体积分数在 0 到 1。数值为此教程结果，适用范围为本机串行算例。 [官方版本](https://github.com/laserbeamfoam/LaserbeamFoam/releases/tag/V3.1) · [构建日志](../../tmp/ff-f64-laserbeamfoam-20261125/build.log) · [数值回执](../../tmp/ff-f64-laserbeamfoam-20261125/numerical-result.json)

两组射线时间序列各有 10 帧。第一组 5 帧包含线段，其余帧和第二组的 10 帧均为空，原样保留。首次打包把射线目录 `VTKs` 拼写为 `VTK`，已从同一次正常结束的求解回收输出并重读。模型调用 0 次。 [求解日志](../../tmp/ff-f64-laserbeamfoam-20261125/plate-v3-results/solve.log) · [回读工具](../server/examples/laserbeamfoam/verify_plate.py)

## OpenRadioss 与 OpenCourant

OpenCourant 项目列出原 OpenRadioss 发布入口于 2026-10-01 下线。原 GitHub 仓库、发行页和 Git 查询在本机不可用。标注为原 `latest-20260615` 的存档镜像可查到发布方元数据，Docker Hub 注册表连接超时。Windows 首次下载 OpenCourant 也超时，非特权 WSL 下载完成并通过发布页 SHA-256。 [社区说明](https://opencourant.org/) · [存档镜像元数据](https://hub.docker.com/layers/dheiny/openradioss-solver/1.0.36/images/sha256-c843e90017e6575bd2cd3f4187fe7b7a7fb31314e81858698497ce80ca16a3b2) · [OpenCourant 发行摘要](https://github.com/OpenCourant/OpenCourant/releases/expanded_assets/latest-20261011)

实际用 UID 65534 运行 OpenCourant `latest-20261011` 的同包 Starter、Engine 和动画转换器。输入为该版本随源码保留的单砖 `Test_traction` 测试，在隔离目录保留原许可证。首次收集漏了原版 `qadiags.inc`，补齐后 Starter 零错误，Engine 正常终止于 13,077 步，保留一个 `/PROP/SOL_ORTH` 末尾字段警告。初末两帧各 8 节点、1 单元，末态四个受载节点位移 1.00006（算例单位），轴向应变 0.693138，与名义终态 `ln(2)` 差 9.18×10⁻⁶，轴向应力与 `100000 × 应变` 差 0.00346（算例单位）。模型调用 0 次。 [算例来源](https://github.com/OpenCourant/OpenCourant/tree/latest-20261011/qa-tests/miniqa/PROP/PROPSOL_ORTH_Istrain) · [OpenCourant 数值回执](../../tmp/ff-f64-openradioss-20261125/numerical-result.json)

随后从 OpenCourant 的归档页下载两份独立副本同摘要的原 OpenRadioss `latest-20260728` Linux x86-64 ZIP。本机核对 82,648,350 B 和 SHA-256 `598ed7b2905a7bacc8d1781470c250ac79d7558c39ba962768edf3644644fe33`。同一输入在原 Starter 和 Engine 上通过，原生的第一帧与最后一帧分别与社区版字节一致。原版日志、两个可执行文件和转换器的摘要、两帧 VTK 数值均独立核对。原进程已退出，模型调用 0 次。本次范围为 OpenRadioss 与 OpenCourant 各指定版本的 Linux SMP 原生路径。 [原版安装包归档](https://github.com/OpenCourant/extlib/releases/tag/upstream-latest-20260728-linux64) · [原 OpenRadioss Starter](../../tmp/ff-f64-openradioss-20261125/upstream/traction/Test_traction_0000.out) · [原版数值回执](../../tmp/ff-f64-openradioss-20261125/upstream/numerical-result.json)

## SPHinXsys

官方 `v1.5_update_sycl_2000` 的固定提交 `64d6069f7469c4171f7807e7e3118a6d7dee06a5` 在非特权 WSL 构建二维 CPU 库和 `test_2d_dambreak`。依赖解包在独立目录。构建配置仅启用这个算例，并把私有 Simbody 包指向系统已有的 BLAS/LAPACK 运行库。首个编译时限前完成 150/152 步，Ninja 复用产物完成余下两步。算例输入和求解源码采用原版。 [官方版本](https://github.com/Xiangyu-Hu/SPHinXsys/releases/tag/v1.5_update_sycl_2000) · [源码摘要](../../tmp/ff-f64-sphinxsys-20261125/native.json) · [构建配置差异](../../tmp/ff-f64-sphinxsys-20261125/configure-target-scope.diff) · [构建结果](../../tmp/ff-f64-sphinxsys-20261125/build.log)

原 `test_2d_dambreak --state_recording=true --regression=false` 跑到算例时间 20.050158。官方随附参考库的压力及总机械能动态时间规整检查均通过。实际生成 197 个流体 VTP 帧。回读首末帧，3,200 个 `OriginalID` 完整，粒子前沿坐标由 1.98750 到 5.36725，最终最大速度 0.81425（算例单位）。压力与能量各有 22 个记录，字段和几何均有限。UID 65534，无残留求解器进程，模型调用 0 次。 [原生求解日志](../../tmp/ff-f64-sphinxsys-20261125/dambreak/solve.log) · [数值回执](../../tmp/ff-f64-sphinxsys-20261125/numerical-result.json) · [回读工具](../server/examples/sphinxsys/verify_dambreak.py)

## FDS

FDS 6.11.2 的 Linux 归档按官方 SHA-256 验证，在 WSL 非特权目录解出内嵌发行包。使用随附 Intel MPI 动态库、共享内存通信和一个 OpenMP 线程运行官方 `heat_conduction_a.fds`。从 20 °C 初值实际计算 4,000 步到 2,000 秒，生成 1,001 个采样和原始 `.out`、`.smv`、`_devc.csv`。六个位置的终温与独立计算的 100 项解析解比较，最大绝对偏差为 0.35536 °C。运行身份为 UID 65534，模型调用为 0。[原始结果](../../tmp/ff-f64-fds-20261124/numerical-result.json) · [验证程序](../server/examples/fds/verify_slab.py) · [官方基准](https://github.com/firemodels/fds/blob/FDS-6.11.2/Verification/Heat_Transfer/heat_conduction_a.fds)
