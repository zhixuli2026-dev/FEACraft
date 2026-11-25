# 求解器源码接入

以 FEACraft 当前托管版本为基线。从固定版本的上游源码构建可调用的库，通过现有算例入口运行，再用原程序和独立数值基准对照。作业仍在独立进程里运行，以沿用现有取消、资源限制、输入封存和结果发布。

| 求解器 | 当前源码接入范围 | 后续覆盖 |
| --- | --- | --- |
| OpenFOAM v2406 | 11 个应用编成共享库，其中 10 个通过字段对照和平台作业。方腔通过两次断点续算及两分区 MPI 对照。 | 自定义代码和其他应用。模块继续链接系统的 v2406 通用库。 |
| CalculiX 2.23 | 从 C、Fortran 和 SPOOLES 源码构建 `libfeacraft_calculix.so`。两档 C3D8 静力、前三阶模态和二进制续算与原 Windows 2.23 一致。Linux API 已发布静力结果。 | Windows 源码构建和拓展平台算例。 |
| SU2 8.5.0 | OpenMP 源码共享库通过 NACA0012 数值对照、Linux 托管作业和已发布检查点的续算。四个场在 5,233 个点的写出值一致。 | 多区域、MPI 和 Windows 源码构建。 |
| Code_Aster 18.1.11 | 已从源码构建并独立打包。轴向杆 MED 与原程序逐值一致，已通过 Windows API 和 WSL 作业发布。计算库已完成真实续算。 | 其他原生算例与物理模型。 |
| Elmer 26.2 | 由源码构建串行库和平台已登记模块。弹性、传热、静电、电流传导、瞬态流动及二进制续算通过数值对照。Linux API 已发布弹性 VTU。 | Windows 源码构建及更多托管作业验收。 |
| MOOSE 2026.11.13 | 完整 combined 应用由源码构建，稳态扩散及传热模块通过原程序对照，并通过 Windows API／WSL 发布和瞬态续算。 | 其他模块的专项算例。 |

REA 使用 `inspect-elf-dependencies` 和 `inspect-pe-imports` 在本地读取文件格式、摘要和动态依赖。原始 JSON 位于 `tmp/native-openfoam-source-20261125/rea-*.json`。这一组记录用于固定二进制基线，数值对照和进程追踪另有回执。 [REA 源码](https://github.com/morluto/rea/tree/26cb5b53ceeb1b3288c430ba1b222c2537457740)

## 修改源码并重建

OpenFOAM 的 `--source-root` 可直接指向修改后的 v2406 源码树。其他五个构建脚本接受附加的 unified diff 文件。脚本先校验官方源码包，再从各求解器的源码根目录按参数顺序应用补丁，并输出补丁摘要。Git 补丁可从对应的上游 checkout 通过 `git diff` 生成。每次构建使用新的输出目录，便于同时保留基线和改动后的构建验收。

```bash
bash platform/native-solvers/calculix/build.sh /path/to/archives \
  /path/to/calculix-custom /path/to/local-solver.patch
```

已用 CalculiX 实际复验修改流程。对上游 C 入口增加一行构建验证信息，重新生成共享库后，两档载荷的真实求解日志都出现了该信息，节点位移仍与原程序逐值一致。 构建日志 `tmp/native-calculix-source-20261125/editable-build.log` · 补丁版位移对照 `tmp/native-calculix-source-20261125/editable-parity/parity.json`

## OpenFOAM

从官方 v2406 源码目录构建，运行主机需装有同版本的 OpenFOAM 头文件、库及 `wmake`。本机校验 `OpenFOAM-v2406.tgz` 的 SHA-256 为 `8d1450fb89eec1e7cecc55c3bb7bc486ccbf63d069379d1d5d7518fa16a4686a`。 [官方源码包](https://dl.openfoam.com/source/v2406/)

```bash
source /usr/lib/openfoam/openfoam2406/etc/bashrc
python3 platform/native-solvers/openfoam/build.py --source-root /path/to/OpenFOAM-v2406 \
  --output /path/to/source-solvers icoFoam laplacianFoam acousticFoam rhoCentralFoam interFoam \
  simpleFoam pimpleFoam buoyantBoussinesqSimpleFoam chtMultiRegionFoam overLaplacianDyMFoam
```

在 API 的部署环境设置 `VIBEFLOW_OPENFOAM_SOURCE_LIBRARY_DIR=/path/to/source-solvers`。该路径以执行主机为准。Windows 使用 WSL 路径，例如 `/mnt/d/FEACraft/tmp/source-solvers`。将上述十个库与 `openfoam-source-libraries.json` 一起部署。平台在受管串行及 MPI 作业中加载库，核对 ABI 和库摘要。串行结果在 `toolchain.source_build` 记录构建，MPI 通过原生回执记录各分区共用的构建。

方腔算例 5 个写出时刻的 `U` 和 `p` 共 10 份场、导热算例的 `T` 场与原应用逐字节一致。追踪记录显示共享库运行只启动了 Python 加载进程。平台导热算例的 80 个单元与解析解在字段写出精度内一致。同一算例的两个独立运行目录也通过了已准备串行批次，两次均载入源码库并生成各自的不可变发布和实际文件查询记录。 方腔文件对照 `tmp/native-openfoam-source-20261125/cavity-parity-runner/parity.json` · 导热文件对照 `tmp/native-openfoam-source-20261125/conduction-parity-runner/parity.json` · 方腔工作流 `tmp/openfoam-source-icofoam-4dwzinj9/source-workflow.json` · 导热工作流 `tmp/openfoam-source-laplacianfoam-ho2c42ky/source-workflow.json` · 已准备串行批次 `tmp/source-prepared-serial-es7tdnbk/source-prepared.json`

`platform/native-solvers/openfoam/verify.py` 可复验字段和进程追踪。`verify_workflow.py` 使用隔离的数据库和文件存储，通过真实 API 重跑完整流程。

`acousticFoam` 的 4 份声压场、`rhoCentralFoam` 的 28 份可压缩流场和 `interFoam` 的 60 份两相流场均与原应用逐字节一致。 声学对照 `tmp/native-openfoam-source-20261125/expanded-acoustic-parity/parity.json` · 可压缩流对照 `tmp/native-openfoam-source-20261125/expanded-compressible-parity/parity.json` · 两相流对照 `tmp/native-openfoam-source-20261125/expanded-dam-parity/parity.json`

`simpleFoam` 的 10 份稳态湍流场、`pimpleFoam` 的 2 份运动网格场、`buoyantBoussinesqSimpleFoam` 的 3 份自然对流场、`chtMultiRegionFoam` 的 4 份多区域温度场和 `overLaplacianDyMFoam` 的 3 份重叠网格温度场均逐字节一致。`overPimpleDyMFoam` 已通过编译。 稳态湍流 `tmp/native-openfoam-source-20261125/expanded-steady-short-parity/parity.json` · 运动网格 `tmp/native-openfoam-source-20261125/expanded-moving-parity/parity.json` · 自然对流 `tmp/native-openfoam-source-20261125/expanded-gravity-parity/parity.json` · 共轭传热 `tmp/native-openfoam-source-20261125/expanded-cht-parity/parity.json` · 重叠网格 `tmp/native-openfoam-source-20261125/expanded-overset-parity/parity.json`

上述五个算例也分别通过平台的已准备串行入口完成发布，每份发布场与独立原程序记录的摘要一致。 稳态湍流发布 `tmp/openfoam-expanded-source-grtsvqgf/source-expanded.json` · 运动网格与自然对流发布 `tmp/openfoam-expanded-source-vz89y0ms/source-expanded.json` · 共轭传热发布 `tmp/openfoam-expanded-source-jdq65bvi/source-expanded.json` · 重叠网格发布 `tmp/openfoam-expanded-source-vhvlws_1/source-expanded.json`

同一份方腔网格用两分区完成分解、并行求解和重组，两次 API 作业分别使用原应用和源码库。重组后的 10 份速度与压力场逐字节一致，源码回执绑定了两分区加载的库摘要及已验证的进程停止。 MPI 对照 `tmp/native-openfoam-source-20261125/mpi-source-parity.json`

分两次取消和恢复实际方腔作业，按 `0.005`、`0.01` 的已写出时间续算至 `0.5`，和不中断的独立作业比较结果场。续算保持共享库及加载入口的指纹，并通过 D 阶段确认。溃坝的真实准备运行发布了 20 个时刻的全部 60 份对照场。 续算验收 `tmp/workflow-checkpoint-FS-SQLite-uessibgz/checkpoint-result.json` · 溃坝发布 `tmp/openfoam-expanded-source-ht36iklq/source-expanded.json`

## CalculiX

将两个官方源码包放在同一目录。构建脚本核验两份 SHA-256，使用 GCC、GFortran、make、Perl、Python 以及系统 ARPACK、LAPACK、BLAS 运行库。 [CalculiX 2.23 源码](https://www.dhondt.de/ccx_2.23.src.tar.bz2) · [SPOOLES 2.2 源码](https://www.netlib.org/linalg/spooles/spooles.2.2.tgz)

```bash
bash platform/native-solvers/calculix/build.sh /path/to/archives /path/to/calculix-source
```

可以直接从共享库调用 C ABI `feacraft_calculix_run(int, char **)`。Linux 平台通过 `VIBEFLOW_CALCULIX=/path/to/calculix-source/ccx` 使用同一套代码。保留该入口、共享库和摘要文件在同一目录。

两档载荷均核验 8 个节点的三个方向位移。与原 Windows 求解结果的最大差为 0，与独立积分组装的刚度方程最大绝对差约为 `3.33e-8`。进程追踪核对新入口和共享库的实际加载。Linux API 验证了带空格的输入路径、已发布 FRD 的摘要和子进程停止回执。 位移对照 `tmp/native-calculix-source-20261125/final-parity/parity.json` · API 回执 `tmp/native-calculix-source-20261125/calculix-source-api.json` · [复验入口](../native-solvers/calculix/verify.py)

原程序与源码库的前三阶模态频率均为 0.6547081、0.6547081 和 0.887067 Hz。两套运行环境各自从二进制检查点恢复，静力位移最大差为 0。 模态与二进制续算对照 `tmp/native-calculix-source-20261125/modal-restart-parity/parity.json`

## SU2

当前构建为 Linux x86-64 单进程 OpenMP，提供 `feacraft_su2_run(int, char **)`。Meson 构建使用 SU2 8.5.0 上游求解源码，为这一组外部依赖固定源码校验值。 [SU2 固定源码](https://github.com/su2code/SU2/tree/12eb826f049ef7f67df974dfcb44cf36ee07c0f8) · [构建脚本](../native-solvers/su2/build.sh)

| 文件名 | 上游源码 |
| --- | --- |
| `SU2-v8.5.0.tar.gz` | [SU2 v8.5.0](https://codeload.github.com/su2code/SU2/tar.gz/refs/tags/v8.5.0) |
| `meson-5a82ea.tar.gz` | [Meson](https://codeload.github.com/mesonbuild/meson/tar.gz/5a82ea0501736a666ca9cc003ea0774f8219fd65) |
| `MEL-46205a.tar.gz` | [MEL](https://codeload.github.com/pcarruscag/MEL/tar.gz/46205ab019e5224559091375a6d71aabae6bc5b9) |
| `eigen-d71c30.tar.gz` | [Eigen](https://gitlab.com/libeigen/eigen/-/archive/d71c30c47858effcbd39967097a2d99ee48db464/eigen-d71c30c47858effcbd39967097a2d99ee48db464.tar.gz) |

```bash
bash platform/native-solvers/su2/build.sh /path/to/archives /path/to/su2-source
```

将 `VIBEFLOW_SU2` 指向产物目录的 `SU2_CFD`。Linux API 使用该入口完成 NACA0012 求解，核验 VTU 摘要及原生进程停止。该验收使用 `OMP_NUM_THREADS=1`。

NACA0012 的两条路径均在第 163 次迭代达到收敛条件。密度残差从约 `-1.027` 降至 `-8.005`。逐点比较密度、压力、温度和马赫数，两者写出的数值一致。进程追踪记录了源码运行入口载入 `libfeacraft_su2.so`。 逐点对照 `tmp/native-su2-source-20261125/final-parity/parity.json` · API 回执 `tmp/native-su2-source-20261125/su2-source-api.json`

使用平台发布的不可变检查点进行真实续算。原检查点摘要保持一致，续算通过同一源码入口生成并发布了新结果。 SU2 续算回执 `tmp/native-su2-source-20261125/su2-source-restart-api.json`

## Elmer

从官方 `release-26.2` 源码构建标准串行入口及其共享库。Linux 构建需要 GCC、GFortran、CMake、Ninja、`patchelf` 和 BLAS/LAPACK。构建包括内置 UMFPACK，以及平台已登记的热、力、流、静电和电流传导模块。 [固定源码包](https://codeload.github.com/ElmerCSC/elmerfem/tar.gz/ab1ecf1b846f5f4e577434e6195153e3581304ed)

```bash
bash platform/native-solvers/elmer/build.sh /path/to/elmer-26.2.tar.gz /path/to/elmer-source
```

Linux 部署将 `VIBEFLOW_ELMER` 指向 `elmer-source/bin/ElmerSolver`。入口加载同一构建中的 `libelmersolver.so`，算例求解器按 `Procedure` 加载同一目录下的模块。轴向应变 `0.001` 和 `0.002` 分别得到 210 MPa 和 420 MPa 应力。12 个节点与 Windows 原程序相比，最大位移差为 `2.17e-19 m`，最大应力差为 `2.98e-7 Pa`。Linux API 校验了已发布 VTU 的摘要及子进程停止回执。 弹性与进程追踪回执 `tmp/native-elmer-source-20261125/final-parity/parity.json` · API 回执 `tmp/native-elmer-source-20261125/elmer-source-api.json`

稳态传热在同一网格上施加 300 K 和 400 K 的端面温度。全部 12 个节点的温度对原 Windows 程序最大差为 `5.68e-14 K`，与解析线性温度场的最大差为 `1.14e-13 K`。追踪记录确认载入了源码编译的 `HeatSolve.so`。 导热对照 `tmp/native-elmer-source-20261125/heat-parity/parity.json`

`StatElecSolve`、`StatCurrentSolve` 与 `StatCurrentSolveVec` 各自在同一网格的两端施加 0 V 和 10 V。三组算例的 12 个节点均与原 Windows 版本逐值一致，解析误差小于 `2.67e-15 V`，并确认载入各自的源码模块。 电势对照 `tmp/native-elmer-source-20261125/electric-parity/parity.json`

上游的自然对流算例加载 `FlowSolve.so` 和二进制检查点，恢复并输出 10 个时间步。最后时刻 841 个节点的温度与原 Windows 版本最大差为 `2.85e-13 K`，速度分量最大差为 `5.87e-17 m/s`。 自然对流与二进制续算 `tmp/native-elmer-source-20261125/convection-restart-parity/parity.json`

## Code_Aster

18.1.11 源码固定到 `87f699afe0706d84fd180e7a84d88bd0556e669c`。Waf 重建了求解库、Python 绑定及 MFront 材料模块。打包按原 Conda 文件清单选取依赖，装入源码构建的 `libaster.so`、`libbibfor.so` 等组件，并写入构建摘要。运行复用现有环境的 Python、MED、HDF5、MUMPS 等依赖。 [上游源码包](https://gitlab.com/codeaster/src/-/archive/87f699afe0706d84fd180e7a84d88bd0556e669c/src-87f699afe0706d84fd180e7a84d88bd0556e669c.tar.gz) · Waf 构建回执 `tmp/native-aster-source-20261125/build-source.log`

```bash
bash platform/native-solvers/code_aster/build.sh /path/to/code-aster-18.1.11.tar.gz \
  /path/to/conda-runtime /opt/feacraft/source-code-aster
```

将 `VIBEFLOW_CODE_ASTER` 指向产物的 `bin/run_aster`。轴向杆的两点 MED 位移与原程序逐值相等，杆端位移为 1 mm。追踪记录确认算例由新运行库中的 Python、`libaster.so` 和 `libbibfor.so` 实际执行。平台回执记录了 MED 的摘要及子进程停止。 MED 与加载轨迹 `tmp/native-aster-source-20261125/final-parity/parity.json` · 平台发布 `tmp/code-aster-source-api-2osy5sjx/source-platform.json`

同一源码构建已从原始发布恢复计算库，使用 `POURSUITE` 将载荷加倍。续算得到 2 mm 位移并发布新的 MED 与计算库。原始发布的计算库摘要保持一致。源码构建的计算库以源码安装清单摘要绑定运行环境。 源码计算库续算 `tmp/code-aster-source-restart-zuj3tv2a/source-restart.json`

## MOOSE

MOOSE 固定到 `08315af82ed0b71e3edf82c04161b2da6a1aff19`。构建复用 libMesh `2026.10.21_69c4f84`、PETSc `3.26.0` 和 MPICH；链接使用本机编译器的 glibc 输入及运行环境的 MPICH。 [上游源码包](https://codeload.github.com/idaholab/moose/tar.gz/08315af82e)

完整 `combined-opt` 已从同一源码树编译。包中随附 59 个从该源码树构建的动态库和运行数据，各文件的打包摘要均通过校验。运行链接到该包的应用库以及兼容依赖前缀的 MPICH。 构建日志 `tmp/native-moose-source-20261125/build-combined-resumed.log` · 包校验 `tmp/native-moose-source-20261125/combined-package-checksums.log` · REA ELF 记录 `tmp/native-moose-source-20261125/rea-combined-source.json`

稳态扩散算例的 11 个节点与原 `moose-opt` 逐值一致，中点为 50。Windows API／WSL 通过新入口发布了 Exodus 和 CSV，摘要和进程停止回执均已核验。瞬态传热模块的 51 个写出时刻完成求解，末态 161 个节点与原 combined 应用逐值一致，与独立解析解的最大差为 `2.613e-4 K`。 扩散对照 `tmp/native-moose-source-20261125/combined-parity/parity.json` · 平台发布 `tmp/moose-source-api-sp8mpnpl/source-platform.json` · 传热场对照 `tmp/native-moose-source-20261125/combined-heat-parity/parity.json`

通过同一个完整应用，将平台在 0.2 秒发布的瞬态检查点续算到 0.4 秒。11 个节点与连续计算逐值相同，中点均为 45.950754454071。检查点作业在 `/tmp` 挂载私有临时文件系统，运行产物部署在 `/tmp` 之外。 完整应用瞬态续算回执 `tmp/moose-source-restart-y9j_t1e2/source-restart.json`

完整应用使用已安装的兼容依赖前缀和 `patchelf` 构建与打包。编译目录可位于 `/tmp`，产物应部署到持久的 Linux 文件系统。

```bash
bash platform/native-solvers/moose/build.sh /path/to/moose-source.tar.gz \
  /path/to/dependencies /path/to/build-workspace /opt/feacraft/moose-source
```

将 `VIBEFLOW_MOOSE` 指向产物的 `bin/combined-opt`。构建目录可以在打包后回收，部署保留完整的 `bin`、`lib`、`share` 及 `SHA256SUMS`。

本页的验收全部运行真实求解器，模型调用为 0。相对链接中的 `tmp/` 是这台验证机的原始回执，构建和复验入口在仓库的 `platform/native-solvers/`。
