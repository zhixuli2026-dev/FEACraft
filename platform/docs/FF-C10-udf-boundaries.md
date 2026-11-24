# FF-A10 / FF-C10 OpenFOAM UDF D1–D5

## 自定义速度边界

算例 UDF 表单支持选择「速度边界条件」。`POST /cases/{id}/udf/scaffold` 使用 `kind: boundaryCondition`，生成 D1–D5 说明、README、类型声明及 `VibeflowVelocityBoundary.C` 与 `Make` 文件。已有同名源文件时会拒绝覆盖。`POST /cases/{id}/udf:build` 读取项目类型并通过实际 `wmake libso` 构建，动态加载后发布 `libvibeflowVelocityBoundary.so` 和原生日志。历史函数对象项目继续使用原格式。 [注册接口](../server/app/routers/udf.py) · [边界模板](../server/examples/udf/boundary/VibeflowVelocityBoundary.C)

在 `0/U` 的选定边界填入 `type vibeflowFixedVelocity`、`velocity (1 0 0)` 和 `value uniform (0 0 0)`。在 `icoFoam` 中，该库按配置实际更新边界系数并写出选定速度。D5 可从独立 UDF 入口执行，也可用 `solve_mode: udf` 和 `udf_name` 进入正式工作流。工作流把新库附加到私有 `controlDict.libs`，保留已有库条目和公共 `controlDict`。 [构建与集成](../server/app/services/udf_handler.py) · [工作流绑定](../server/app/services/workflow_udf_handler.py)

OpenFOAM 2406 中真实编译并运行 400 单元方腔。`movingWall` 使用 1 m/s，原生输出记录实际边界类型和速度。末时刻 0.5 s 的内部速度和压力与独立固定速度参照逐字节一致。文件系统通过脚手架、原生编译、加载、求解及 D 确认，并拒绝重复脚手架。原生工具和托管作业使用 WSL 非特权运行。 [原生对照](../../tmp/FF-C10-boundary-20261123/native.json) · [文件系统](../../tmp/ff-c10-udf-boundary-4x4z2l9f/boundary.json)

Edge 在 1440、900 和 640 像素宽度用键盘操作类型选择和 D1–D5。实际作业完成 `wmake` 和 `icoFoam`，归档库与工作流编译结果摘要一致。 [Edge 实测](../../tmp/cb08-h18-edge-udf-boundary-az7yhxev/browser.json)

认证对象存储也已通过完整 B–D。边界库、末态内部速度和末态压力的摘要与文件系统一致。首轮测试客户端在默认等待时限内未收到回复，按仓库其他远端 UDF 验收的客户端设置复验通过。 [认证对象存储](../../tmp/ff-c10-udf-boundary-3sont77e/boundary.json) · [临时 PostgreSQL 清理](../../tmp/terminal-agent-postgres-rnhjsh79/cleanup.json)

## 自定义动量源项

脚手架选择 `kind: sourceTerm`。项目在 `constant/fvOptions` 或 `system/fvOptions` 启用 `vibeflowUniformAcceleration`，系数中的三分量 `acceleration` 使用 m/s²。由真实 `wmake libso` 构建的源项注册到串行 `pimpleFoam` 的速度方程。源项按单元体积写入离散方程。工作流保留公开的 `controlDict` 和 `fvOptions`。 [源项模板](../server/examples/udf/source/VibeflowAcceleration.C)

32 单元的周期通道从静止出发，施加 5 m/s²。实际原生时刻 0.1 s 和 0.2 s 的速度分别为 0.5 m/s 和 1.0 m/s，逐单元解析误差低于 `1.5 × 10⁻¹⁶ m/s`。文件系统工作流确认 B–D，实际构建和集成已退出，两个时刻的场文件摘要与独立原生求解一致。 [独立原生](../../tmp/FF-C10-source-term-20261123/native.json) · [文件系统](../../tmp/ff-c10-udf-source-lw4w5ezq/source.json)

认证对象存储同样通过实际 B–D。库、0.1 s 和 0.2 s 速度场的摘要与文件系统一致。已删除临时 PostgreSQL 数据库。 [认证对象存储](../../tmp/ff-c10-udf-source-z1zkqpzn/source.json) · [临时 PostgreSQL 清理](../../tmp/terminal-agent-postgres-dl78mkoi/cleanup.json)

## 自定义求解器

脚手架选择 `kind: solver`，生成 `vibeflowDiffusionFoam.C`、Make 文件和 D1–D5。算例在 `controlDict.application` 选择 `vibeflowDiffusionFoam`，并提供 `0/T` 和 `constant/transportProperties.DT`。D4 由真实 `wmake` 链接原生可执行文件，通过 `-help` 核验入口。D5 使用同一封存可执行文件在 WSL 非特权工作区求解。 [求解器模板](../server/examples/udf/solver/vibeflowDiffusionFoam.C)

真实 32 单元热扩散在 0.2 s 和 0.4 s 与 `laplacianFoam` 的温度逐项一致。文件系统的真实工作流确认 B–D，两时刻原生文件与独立运行的摘要一致。首轮 `wmake` 成功，链接证据检查只接受动态库导致拒绝。现在核验实际可执行文件的链接目标。后续独立插入真实编译报错并核验进程退出、发布拒绝，恢复源码后重新构建和求解成功。 [独立原生](../../tmp/FF-C10-custom-solver-20261123/native.json) · [首次失败日志](../../tmp/ff-c10-udf-solver-8x1uqd4q/files/workspaces/1/cases/1/.vibeflow/jobs/job-4.log) · [文件系统与恢复](../../tmp/ff-c10-udf-solver-37sjin3m/solver.json)

PostgreSQL 与认证 SeaweedFS 的真实工作流也确认 B–D，两时刻温度场和可执行文件与文件系统逐字节一致。Edge 通过三个窗口宽度的键盘操作，实际执行独立 D4 和 D5。 [认证对象存储](../../tmp/ff-c10-udf-solver-mosnzvxf/solver.json) · [临时 PostgreSQL 清理](../../tmp/terminal-agent-postgres-ktgtreru/cleanup.json) · [Edge 实测](../../tmp/cb08-h18-edge-udf-solver-hknvevuy/browser.json)

## 自定义湍流模型

脚手架选择 `kind: turbulenceModel`。在 `constant/turbulenceProperties` 启用 `RASModel vibeflowKEpsilon`，在 `vibeflowKEpsilonCoeffs` 中指定正数 `effectiveCmu`。模型复用 OpenFOAM k–ε 输运方程并覆盖黏度系数。D4 生成 `libvibeflowKEpsilon.so`，D5 使用串行 `pimpleFoam`，读入算例的 `0/k`、`0/epsilon` 和 `0/nut`。 [湍流模型模板](../server/examples/udf/turbulence/VibeflowKEpsilon.C)

独立原生 `wmake libso` 与 `pimpleFoam` 已在 32 单元网格运行三组。系数 `0.06` 的自定义模型与同系数标准模型各单元一致，湍流黏度为系数 `0.09` 标准模型的三分之二。0.1 s 和 0.2 s 的逐单元公式误差均小于 `6 × 10⁻¹⁴ m²/s`。 [三组原生对照](../../tmp/FF-C10-turbulence-20261123/native.json) · [G5 原任务范围](implementation-master-plan.md)

首轮两套存储的 D4 成功，D5 在派发前停住。冷启动诊断确认当前编译来源把刚发布的 `libvibeflowKEpsilon.so` 从缺失记录变为输入文件。将该注册输出纳入现有编译缓存过滤后，文件系统通过 B–D，两时刻的 `k`、`epsilon` 和 `nut` 与独立原生结果逐字节一致。0 系数被拒绝后，原归档仍可读。 [首轮文件系统](../../tmp/ff-c10-udf-turbulence-5pcptazr/turbulence.json) · [首轮对象存储](../../tmp/ff-c10-udf-turbulence-aautrx3w/turbulence.json) · [冷启动派发诊断](../../tmp/ff-c10-turbulence-dispatch-mzbzrrxf/dispatch.json) · [文件系统复验](../../tmp/ff-c10-udf-turbulence-x5to5esn/turbulence.json)

认证 SeaweedFS 的实际 B–D 同样通过。库及六份结果文件与文件系统逐字节一致。Edge 首轮测试在实际编译等待期间超时。面板现按作业终态等待，期间显示作业编号，离开算例时停止页面查询。复验在三个窗口宽度通过键盘执行 D1–D5，接口读取的动态库摘要与托管工作流一致。 [认证对象存储](../../tmp/ff-c10-udf-turbulence-x7yw4m64/turbulence.json) · [临时 PostgreSQL 清理](../../tmp/terminal-agent-postgres-0o99enys/cleanup.json) · [Edge 复验](../../tmp/cb08-h18-edge-udf-turbulence-tuuzfg_u/browser.json)

现行 OpenFOAM `RAS.model` 和历史 `RASModel` 均可指定同一自定义模型。使用现行字段的文件系统实算通过，两个字段指定不同模型时在集成前拒绝。 [实算和冲突回执](../../tmp/ff-c10-udf-turbulence-rxm_08_z/turbulence.json)

## 函数对象

界面显式选择「函数对象」时，脚手架提供项目 README。省略 `kind` 的旧调用保留原九份文件。新版文件系统通过真实编译、动态加载、方腔求解与 E 确认。400 个单元的运动学压力均值与函数对象输出相差约 `7.17 × 10⁻¹⁴ m²/s²`，归档场与认证对象存储的既有原生结果一致。 [文件系统复验](../../tmp/ff-c14b-udf-pressure-jrznex6y/udf-pressure.json) · [认证对象存储](../../tmp/ff-c14b-udf-pressure-35v92x01/udf-pressure.json)

## T04 验收

2026-11-23，FF-A10 与 FF-C10 完成原 G5。五类项目均有 D1–D5 文件、真实构建和加载、实际 OpenFOAM 集成、封存产物与独立物理对照。矩阵还包含真实编译报错与修复，以及两套存储的编译、加载及求解阶段实际取消和恢复证据。本阶段计算与浏览器复验调用模型 0 次。既有取消恢复记录单独保留真实 DeepSeek 对实际证据的回读。 [G5 汇总](../../tmp/FF-C10-turbulence-20261123/g5-validation.json) · [取消恢复](udf-running-cancel-recovery.md) · [台账](../../tmp/stage-ledger-evidence-20261123-g5-udf.json)
