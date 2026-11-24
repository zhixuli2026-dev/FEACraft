# FF-CB10 线性求解、耦合、时间控制与 MPI 分解

`caseDict` 的 `numericalMethod.linearSolvers` 按字段声明原生算法、预条件器或平滑器、绝对和相对容差以及可选的迭代上限。已有 `fvSolution` 时核对声明值并保留原件。`laplacianFoam` 可以从 `T` 字段声明生成私有 `fvSolution`，用于托管网格检查和实际求解。封存输入、结果证据和标准算例包记录实际配置。 [OpenFOAM fvSolution](https://doc.openfoam.com/2306/fundamentals/case-structure/fvsolution/)

`numericalMethod.timeControl` 的 `endTime`、`deltaT`、`writeControl` 和 `writeInterval` 核对原生 `controlDict`，保持其中其他配置。两种存储的 64 单元导热算例实测在 0.5 s、1 s 写出，使用 `DICPCG`，温度与解析解一致。托管和离线包两处结果的字段逐字节一致。 [文件系统](../../tmp/ff-cb10-numerical-tw5zm9qh/numerical.json) · [认证对象存储](../../tmp/ff-cb10-numerical-ljotgkpb/numerical.json) · [OpenFOAM controlDict](https://doc.openfoam.com/2312/fundamentals/case-structure/controldict/)

相同算例还实测了 `PBiCGStab` 与 `diagonal`、`smoothSolver` 与 `symGaussSeidel`、`GAMG` 与 `GaussSeidel`。原生已有配置与声明一致时，工作副本及离线包保留原字节。时间配置冲突和原生线性容差冲突均在 C 被拦截，未启动求解。表单提交负相对容差返回 422，算例修订未改变。 [PBiCGStab](../../tmp/ff-cb10-numerical-3cluznj7/numerical.json) · [smoothSolver](../../tmp/ff-cb10-numerical-mdlkzus7/numerical.json) · [GAMG](../../tmp/ff-cb10-numerical-lekyi3sa/numerical.json) · [保留原生配置](../../tmp/ff-cb10-numerical-n6a57pks/numerical.json) · [时间冲突](../../tmp/ff-cb10-numerical-csusnasn/numerical.json) · [线性容差冲突](../../tmp/ff-cb10-numerical-t1u_gpc2/numerical.json)

首轮真实运行发现 `checkMesh` 需要 `fvSolution`，现在网格工作区和求解工作区使用同一份生成规则与摘要。原试验的 80 单元网格违反既有完整质量检查，实际对照改用通过检查的 64 单元立方网格。 [首轮缺少原生字典](../../tmp/ff-cb10-numerical-b2ivx222/numerical.json) · [首轮网格质量失败](../../tmp/ff-cb10-numerical-767iye8l/numerical.json)

`numericalMethod.convergence { requireLinearConvergence true; }` 按每条实际线性求解残差核对绝对和相对容差。DICPCG 的最大最终残差为 `1.32720973186e-13`，两次线性求解均通过。最大迭代次数为 1 时，原生程序执行到 `End`，实际残差约为 `0.305516`、`0.080427`。算例停在 C，离线 `Allrun` 返回失败并保存运行证据。两套存储的正反例及离线包均通过。认证对象存储首次负例的阶段读取遇到 `file_sync_busy`，复验等待同步完成后核对当前阶段。 [本地通过](../../tmp/ff-cb10-numerical-dbtvy12_/numerical.json) · [本地拦截](../../tmp/ff-cb10-numerical-n968teis/numerical.json) · [对象存储通过](../../tmp/ff-cb10-numerical-9rswlo91/numerical.json) · [对象存储拦截](../../tmp/ff-cb10-numerical-bsl_akgh/numerical.json) · [原生残差概念](https://doc.openfoam.com/2312/tools/processing/numerics/solvers/residuals/)

两种存储的原生求解在写出 `0.5/T` 后取消，进程范围退出已核验。SQLite 从该快照续算到 20 s，认证对象存储以 0.001 s 的时间步续算到 2 s。检查点字段和 `fvSolution` 摘要一致，温度解析误差为 0，均确认 D。第一次本地测试读错了回执的来源字段位置。认证对象存储首次续算超出测试等待，复验采用较疏的写出间隔。 [实际本地续算](../../tmp/ff-cb10-numerical-hn8yfstx/numerical.json) · [对象存储首轮回执](../../tmp/ff-cb10-numerical-8wsa_q93/numerical.json) · [实际对象存储续算](../../tmp/ff-cb10-numerical-w9jsm2hs/numerical.json)

## PIMPLE 外迭代、对流格式和自适应步长

`timeControl` 可声明 `adjustTimeStep`、`maxCo`、`maxDeltaT`。平台核对与原生 `controlDict` 一致，保存求解日志实测的时间步统计，并用最后一个实际步长检查终止时刻。两套存储的 36 单元二维腔体均报告 15 个步长，范围为 0.0025–0.005 s，实际写出 0.02、0.04、0.06 s，离线包重跑的 `U` 与各自发布逐字节一致。`maxCo` 冲突在 C 被拦截。认证对象存储首轮导出时混用了两版应用模块，固定源码复验成功。 [本地自适应](../../tmp/ff-cb10-adaptive-time-kgckbyox/adaptive.json) · [自适应配置冲突](../../tmp/ff-cb10-adaptive-time-t_q4ft3p/adaptive.json) · [认证对象存储](../../tmp/ff-cb10-adaptive-time-k4l5xjvn/adaptive.json)

`nonlinear` 声明 PIMPLE 外迭代、压力校正和非正交校正，核对 `fvSolution`。`stabilization.momentumConvection` 声明动量对流格式，核对 `fvSchemes`。实际 `linear` 和 `upwind` 均使用两次外迭代，15 步共 30 次外迭代。最终最大速度分别为 0.4411011874 和 0.4274634915 m/s。原生配置缺少声明要求的 `nOuterCorrectors` 时，发布流程记录输入错误且求解保持未启动。 [linear](../../tmp/ff-cb10-adaptive-time-d78glmho/adaptive.json) · [upwind](../../tmp/ff-cb10-adaptive-time-k4l5xjvn/adaptive.json) · [耦合配置冲突](../../tmp/ff-cb10-adaptive-time-hd5yiz_6/adaptive.json)

同一网格的 `linearUpwind` 实测最终最大速度为 0.4443392211 m/s，离线场逐字节一致。Edge 从确定的发布版本读取实际时间步和 PIMPLE 迭代，验证添加示例只编辑当前草稿，并可删除。 [linearUpwind](../../tmp/ff-cb10-adaptive-time-3k7zjq8q/adaptive.json) · [Edge 回执](../../tmp/ff-cb10-adaptive-time-3k7zjq8q/numerical-browser.json)

## MPI 分解

`numericalMethod.decomposition` 支持 `simple` 三轴分区和 `scotch` 图分区。MPI 请求的 rank 数须符合声明。已有方法、数量和轴向布局一致的 `decomposeParDict` 按原字节使用。缺少时在尝试内生成，正式工作流的算例源文件保持原状。封存后由 `foamDictionary` 核对实际 rank 数，读取原生分解日志核对方法，并验证 rank、重构和字段证据。

真实的二维腔体用 `simple` 的 `(1 2 1)` 把 Y 坐标切成 0–0.05 m、0.05–0.1 m 两个区域。手写原生字典的布局和重构速度场一致。认证对象存储用 `scotch` 完成真实两 rank 分解、求解与重构并确认 D。 [本地分解](../../tmp/ff-cb10-decomposition-7aq6_nrn/decomposition.json) · [已有原生配置](../../tmp/ff-cb10-decomposition-q0cn163h/decomposition.json) · [认证对象存储](../../tmp/ff-cb10-decomposition-wdc__5mu/decomposition.json)

认证对象存储重复运行 `simple`，两个分区的几何范围和重构速度场与本地一致。同一腔体使用 `scotch`，实际速度分量最大差为 `3.53e-5` m/s。错误 rank 数和不匹配的三轴布局均复验为 C 步骤阻止，没有创建尝试。首轮负例虽未启动求解，但错误类型未被工作流记录。改用现有输入阻止类型后，两条拒绝路径均留下状态和原因。Edge 验证编辑分区、错误提示、保留正确设置及版本化证据读取。 [对象存储 simple](../../tmp/ff-cb10-decomposition-zi4rmytp/decomposition.json) · [rank 冲突](../../tmp/ff-cb10-decomposition-w_uzleoh/decomposition.json) · [布局冲突](../../tmp/ff-cb10-decomposition-s41xa1jz/decomposition.json) · [Edge 回执](../../tmp/ff-cb10-decomposition-vrnjche7/numerical-browser.json)

两套存储用同一份 64 单元温度算例组合 `simple`、声明式 PCG/DIC、时间控制和严格容差门禁。实际 MPI 正常完成两个时间步并重构温度场，最大最终残差 `5.27336190613e-13`，温度解析误差为 0 K，确认 D。两个后端用于求解的私有 `fvSolution` 摘要一致。把迭代上限设为 1 后，实际两个最终残差约为 `0.423685` 和 `0.0975842`，未发布。共享配置和日志验证接入后复验串行导热和瞬态 PIMPLE，离线结果保持一致。 [MPI 本地组合](../../tmp/ff-cb10-numerical-gigptc2y/numerical.json) · [MPI 对象存储组合](../../tmp/ff-cb10-numerical-lpgt9wqp/numerical.json) · [MPI 残差超限](../../tmp/ff-cb10-numerical-qbfqkr45/numerical.json) · [串行导热复验](../../tmp/ff-cb10-numerical-ha_8obun/numerical.json) · [PIMPLE 复验](../../tmp/ff-cb10-adaptive-time-42unkvin/adaptive.json)

2026-11-22，FF-CB10 完成 T02。旧 `/mpi:run` 的两次真实求解、分区保护和幂等复验通过。当前功能沿用原工作流、所有权检查和数据格式，无数据库迁移。移除可选数值声明即可恢复原生字典直接配置。各物理后端的完整数值支持矩阵在 FF-C02 继续记录。本专项真实模型调用 0，模拟 0。 [旧 MPI 入口](../../tmp/ff-c06b-mpi-force-i_70f5kb/mpi-force.json) · [33 组验收回执](../../tmp/FF-CB10-numerical-20261122/validation.json)

## FF-C02 CalculiX 静力求解器与增量

在 `numericalMethod.calculix.linearSolver` 选择 `spooles`、`iterativeScaling` 或 `iterativeCholesky`。平台核对现有 `*STATIC, SOLVER=…`，缺少时写入私有输入，并从原生日志核验实际求解器及迭代退出条件。 [CalculiX 2.23 手册](https://www.dhondt.de/ccx_2.23.pdf) · [配置和证据校验](../server/app/services/calculix_numerical.py)

启用 `*STEP,NLGEOM` 后，`staticIncrements` 可选 `mode fixed` 或 `mode adaptive`。两种模式填写 `initial` 和 `period`，自适应模式还填写 `minimum` 和 `maximum`。平台按原生 `.sta` 核对收敛增量和完成时刻。固定比值非法、缺少 NLGEOM、原生 `DIRECT` 冲突和求解器冲突均在启动前拒绝。

文件系统和认证对象存储分别实际运行三种线性方法及两种非线性增量策略。固定模式为 4 个 0.25 增量，自适应模式为 6 个增量，每次 Newton–Raphson 最多 2 次迭代。封存输入、STA、DAT 与独立原生运行逐字节一致。既有单向流固作业实算回归通过。 [文件系统](../../tmp/ff-c02-calculix-numerical-__k0t99l/numerical.json) · [认证对象存储](../../tmp/ff-c02-calculix-numerical-8n6rmup5/numerical.json) · [压力传递回归](../../tmp/ff-c10-udf-pressure-transfer-o_otc03o/pressure-transfer.json)

## FF-C02 Elmer 热求解与 BDF 时间控制

`numericalMethod.elmer` 绑定单个原生 `HeatSolve`。`heatSolver` 指定 SIF 编号，`linear.method` 支持 `Banded` 或 `CG`。CG 选择 `None` 或 `Diagonal` 预条件，并填写 `tolerance`、`maxIterations`。可用 `nonlinear` 声明容差和迭代上限。`timeControl` 的 `fixed` 和 `adaptive` 使用一阶 BDF。自适应填写 `minStep`、`maxStep` 和 `errorTolerance`。原生设置一致时保留，缺项在封存副本生成。 [Elmer 求解器手册](https://ftp.csc.fi/pub/files/index/elmer/doc/ElmerSolverManual.pdf) · [配置与证据实现](../server/app/services/elmer_numerical.py)

11 节点导热算例分别实跑直接法、稳态 CG、固定和自适应瞬态。自适应日志记录 77 次试步，完成五个时间步至 1 s，实际线性迭代峰值 9，非线性迭代峰值 2。双存储的数值内容与独立原生相同，原始归档分别保存含时间戳的摘要。把 CG 迭代上限设为 1 时原生实际退出，已有成功发布保留。首轮脚本使用了错误的状态文件目录，后续识别原生注释差异并修正 Windows 日志换行解析。 [文件系统](../../tmp/ff-c02-elmer-numerical-y056bldn/numerical.json) · [认证对象存储](../../tmp/ff-c02-elmer-numerical-_smilmyz/numerical.json)

## FF-C02 SU2 残差、CFL 与物理时间步

`numericalMethod.su2` 校验封存的单区域隐式 Euler `.cfg`，包含 `linear` 的 FGMRES／ILU、固定或自适应 `cfl` 和一个 `steady` 或 `unsteady` 块。稳态匹配 `ITER`、密度对数残差及起始检查迭代。瞬态匹配一阶双时间步、物理时间步和内迭代上限。后端从原生 CSV 核对残差、迭代和 CFL，保持已提交 `.cfg`。 [SU2 8.5 配置](https://github.com/su2code/SU2/blob/v8.5.0/config_template.cfg) · [实际证据读取](../server/app/services/su2_numerical.py)

NACA0012 稳态在第 26 次迭代达到 `-3`，末值 `-3.041173913`。自适应局部伪时间 CFL 在 100 次迭代内由 2 升至 20。固定物理时间步为 0.0001 s，共 4 步，CSV 的最后零基时间标签为 0.0003 s。也可使用 `unsteady.courantNumber` 让 SU2 按物理 CFL 计算时间步。该算例用物理 CFL 0.5 完成 8 步，实际步长持续为 `8.201089489 × 10⁻⁸ s`。

应用要求稳态必须达到 `-3` 时，实际局部 CFL 自适应样例末值 `-1.759339102`，封存核验拒绝发布并保留已有历史。双存储与独立原生在同样单线程配置逐行相同。初次用不同线程数作对照出现 26 和 27 次迭代差异，统一线程数后复验。 [文件系统](../../tmp/ff-c02-su2-numerical-soirw8gt/numerical.json) · [认证对象存储](../../tmp/ff-c02-su2-numerical-3to5li3c/numerical.json) · [线程不一致记录](../../tmp/ff-c02-su2-numerical-jh4tam9y/numerical.json)

## FF-C02 MOOSE 瞬态 Newton 与迭代自适应步长

`numericalMethod.moose` 把线性容差、非线性容差及各自迭代上限与原生 `[Executioner]` 对照，覆盖 `Transient`、`NEWTON`、PETSc LU 和 `IterationAdaptiveDT`。固定模式核对 `dt` 和 `end_time`。自适应模式另核对最小与最大步长、目标迭代数、增步和缩步系数。平台从原生 CSV 和日志核验末时刻、步长范围与收敛。 [MOOSE 步长说明](https://mooseframework.inl.gov/moose/source/timesteppers/IterationAdaptiveDT.html) · [配置与回执](../server/app/services/moose_numerical.py)

一维瞬态扩散固定 8 步与自适应 5 步分别到达 0.4 s。自适应步长在 0.0125–0.15 s，两个存储的 CSV 都与独立 WSL 运行逐字节一致。声明的步长配置用于新运行。原生检查点入口另有双存储续算验收。 [文件系统](../../tmp/ff-c02-moose-numerical-iq_u4a7u/numerical.json) · [认证对象存储](../../tmp/ff-c02-moose-numerical-yficnvog/numerical.json) · [检查点](open-solver-native-runtime.md#moose-检查点续算)

## FF-C02 Code_Aster 静力、Newton 增量与 MUMPS

`numericalMethod.codeAster` 核对原生顶级字面量 `MECA_STATIQUE` 或 `STAT_NON_LINE`。两者配置 MUMPS 线性相对容差，后者可声明 Newton 容差、迭代限制和 `DEFI_LIST_REEL` 时间点。自适应提交用 `DEFI_LIST_INST` 核对初始增量、上下限与增长百分比。首个正时间锚点要与声明的初始增量一致。平台保持 `.comm` 原文，逐增量读取 `.mess` 的完成状态及相对残差。 [原生命令手册](https://code-aster.org/doc/v16/commande.html) · [输入与原生证据校验](../server/app/services/code_aster_numerical.py)

轴向拉伸在两套存储及独立原生运行中均得到约 1 mm 端点位移。线性方法原生报告 MUMPS，固定非线性运行收敛 4 个增量，自适应收敛 5 个，实际范围 0.1–0.3，最大相对残差约 `1.17 × 10⁻¹⁶`。首轮 `.mess` 解析误把总迭代汇总计入单步，修复读取范围后复验。当前声明用于新运行，原生 `POURSUITE` 计算库续算另有双存储验收。 [文件系统](../../tmp/ff-c02-aster-numerical-36883vax/numerical.json) · [认证对象存储](../../tmp/ff-c02-aster-numerical-nu2qrs01/numerical.json) · [计算库续算](open-solver-native-runtime.md#code_aster-计算库续算)

## FF-C02 T04 验收

OpenFOAM、CalculiX、Elmer、SU2、MOOSE 和 Code_Aster 均完成原生输入校验、双存储求解及独立数值对照。T04 的取消和恢复使用带已核验停止证明的 OpenFOAM 实际检查点，两套存储都从 0.5 s 续算至 20 s，温度对照最大绝对差为 0 K。原生输出按各后端的文件清单和摘要封存。此轮实算与浏览器验收调用 LLM 0 次，模拟 0 次。 [T04 矩阵](../../tmp/FF-C02-su2-numerical-20261123/t04-validation.json) · [文件系统续算](../../tmp/ff-cb10-numerical-hn8yfstx/numerical.json) · [认证对象存储续算](../../tmp/ff-cb10-numerical-w9jsm2hs/numerical.json)

Edge 用键盘读取六个实际作业的数值证据，覆盖 1440、900 和 640 像素视口。首轮脚本覆盖了算例名称，随后桌面作业分栏在切回宽屏时需要重新拉高才能看见列表。修正脚本后，六个发布都完成实际读回，包含 SU2 物理 CFL 0.5 和 8 个物理时间步。生产前端构建通过。C 盘可用空间从 17,684,963,328 B 到 17,684,770,816 B。Docker 占用快照一致，本次内部回收 0 B。 [Edge 回执](../../tmp/ff-c02-edge-numerical-9beqya2y/edge.json) · [SU2 桌面](../../tmp/ff-c02-edge-numerical-9beqya2y/su2-physical-1440.png) · [MOOSE 窄屏](../../tmp/ff-c02-edge-numerical-9beqya2y/moose-640.png) · [构建与磁盘](../../tmp/FF-C02-su2-numerical-20261123/build-disk-final.json)
