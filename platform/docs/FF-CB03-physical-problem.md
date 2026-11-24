# FF-CB03 物理问题配置

公开 caseDict 保存已校验坐标系枚举、重力开关和三分量矢量，以及参考长度、面积、密度、压力、温度和速度。重力启用需要显式矢量。压力与温度允许有限的带符号标量，参考速度可为有限标量或三分量矢量。标准耦合开关及流体、结构、传热、电磁和声学控制模型的 `enabled` 按布尔值校验。自定义扩展字段保留。 [StandardCase 结构](../../extracted/share/vibeflow/StandardCase/0-caseDict/caseDict) · [校验实现](../server/app/services/casedict_schema.py)

SQLite 文件系统及 PostgreSQL 认证 SeaweedFS 各通过公开写入和读回。非法坐标系、重力矢量、非法参考量、耦合或模型开关返回 422，原 caseDict、摘要和修订保持。缺少已启用的重力矢量单独拒绝。隔离 API、对象存储及临时库已收尾，真实模型 0，模拟 0。 [实际配置验收](../../tmp/FF-CB03-physical-schema-20261122/validation.json)

后续各节记录每种声明对应的原生输入、数值和实际作业回执。

## 2026-11-22 OpenFOAM 串行重力映射

托管串行 OpenFOAM 在封存时读取 `physicalProblem.gravityEnabled`。启用使用声明的 SI 加速度矢量，关闭使用零矢量。缺少 `constant/g` 时生成 `uniformDimensionedVectorField` 作为作业私有输入。已有 `g` 或 `g.gz` 的矢量与声明一致时直接使用，冲突时拒绝提交。确认 D 时根据当前字典重新生成并验证私有 `g` 的摘要与原求解回执。既有续算封存协议保留该输入和派生记录。 [OpenFOAM 输入生成](../server/app/services/solve_artifacts.py) · [结果门禁](../server/app/services/solve_publication.py)

实际 OpenFOAM v2406 `buoyantBoussinesqSimpleFoam` 在 SQLite 文件系统、PostgreSQL 文件系统及 PostgreSQL 认证 SeaweedFS 读取封存的 `g` 并完成 D 确认。256 单元的热梯度对照使用相同初始 `T` 字节，启用重力的最大速度约 0.54364 m/s，关闭重力时为 0 m/s。工作副本仍可由用户审阅并保存自己的 `constant/g`。首轮在模型实际求解及发布后，确认 D 错将私有 `g` 视为工作副本中的必需文件，修复后复验通过。隔离存储、API 和临时库均已收尾。真实模型 0，模拟 0。 [实际浮力求解与对照回执](../../tmp/FF-CB03-physical-schema-20261122/gravity-validation.json)

本地文件系统另在写出时刻 12 后取消真实原生作业。停止范围核验通过，选择时刻 12 继续至 240。续算读取原有 `g` 摘要，发布速度场后完成 D 确认。 [实际取消和续算](../../tmp/ff-cb03-openfoam-gravity-mjxdpegf/gravity.json)

## 几何坐标重力

`physicalProblem.gravityCoordinates geometry` 使用 `geometry.coordinateSystem` 的三个正交轴。程序将轴归一化，把局部 SI 重力分量投影到全局 `constant/g`。该选项用于固定惯性坐标。缺轴或不正交时拒绝保存，现有原生 `g` 与投影结果冲突时，算例停在 C。默认全局分量保留原行为。 [拦截回执](../../tmp/ff-cb03-openfoam-gravity-_euc2mb2/gravity.json)

本地坐标 `(-9.81, 0, 0)` 在实际算例中投影为 `(0, -9.81, 0)`。SQLite 与认证 SeaweedFS 读到相同 `g` 摘要，速度场与直接指定全局重力的算例逐字节一致，最大速度约 0.54364 m/s，均确认 D。标准包保留生成的 `g`，离线速度场再次核对为同一份字节。 [文件系统](../../tmp/ff-cb03-openfoam-gravity-9zjpdcn1/gravity.json) · [认证对象存储](../../tmp/ff-cb03-openfoam-gravity-3ncdj3xf/gravity.json) · [全局对照](../../tmp/ff-cb03-openfoam-gravity-a9a3s885/gravity.json) · [结果汇总](../../tmp/FF-CB03-physical-schema-20261122/gravity-frame-validation.json)

## 浮力参考温度

`physicalProblem.referenceTemperatureRole buoyancy` 将已有 `referenceTemperature` 按 `caseMeta.units.temperature` 换算成 K。托管 Boussinesq 求解使用相同的 `transportProperties.TRef`。已有且相符的原生值保持原件，冲突时算例停在 C。原生字典缺少该项时在私有封存副本补齐，工作副本原件仍按原摘要核验。离线包保存原始字典，并在独立运行目录应用同一派生文件。 [OpenFOAM 浮力方程](https://api.openfoam.com/2406/solvers_2heatTransfer_2buoyantBoussinesqSimpleFoam_2createFields_8H.html)

本地普通字典及认证对象存储的 gzip 字典均将 36.85°C 换算为 310 K，通过托管和离线运行。压力场与手工原生 310 K 对照逐字节一致。与原生 300 K 算例对比，运动学压力最大变化为 1.1037 m²/s²，速度字段保持相同。低于绝对零度的声明返回 422，原生 300 K 与所声明 310 K 冲突时在创建求解作业前拒绝。首轮准备同时选中了原始和派生字典，现已仅封存派生副本，原字典摘要仍用于校验。 [文件系统](../../tmp/ff-cb03-openfoam-gravity-jn9i524r/gravity.json) · [认证对象存储 gzip](../../tmp/ff-cb03-openfoam-gravity-kxvtlzpa/gravity.json) · [原生温度对照](../../tmp/ff-cb03-openfoam-gravity-m_80p321/gravity.json) · [冲突](../../tmp/ff-cb03-openfoam-gravity-qs88t4ai/gravity.json) · [验收汇总](../../tmp/FF-CB03-physical-schema-20261122/reference-temperature-validation.json)

## 压力波声学介质

在 `physicalProblem.referenceDensity` 填写 kg/m³。把 `governingModels.acoustic.formulation` 设为 `pressureWave`，并在 `soundSpeed` 中填入 `value` 和 `unit`。声速支持 `m/s` 和 `km/s`。原生 `acousticFoam` 用这些设置生成 `constant/transportProperties` 的 `c0` 和 `rho`。现场已有同名原生字典时，必须与声明相符。托管求解会封存该输入，标准 OpenFOAM 包可包含同一原生字典。 [声学介质生成](../server/app/services/openfoam_acoustic_medium.py)

文件系统通过实际工作流将 `0.1 km/s` 与 `1.2 kg/m³` 转换为封存输入，原生求解的 128 个末态声压与直接使用 `100 m/s` 字典的算例逐单元一致。B–D 确认、封存摘要和公开目录无生成副本均已核验，`m2/s` 声速单位在保存前拒绝。 [文件系统](../../tmp/ff-cb08-acoustic-impedance-scne81tl/impedance.json)

PostgreSQL 与认证 SeaweedFS 同样确认 B–D。封存介质字典与原生末态声压的摘要均与文件系统逐字节相同。 [认证对象存储](../../tmp/ff-cb08-acoustic-impedance-02d1n0wu/impedance.json) · [临时 PostgreSQL 清理](../../tmp/terminal-agent-postgres-594j3tvw/cleanup.json)

单独下载标准 OpenFOAM ZIP，确认包内介质字典等于同一封存版本。在 WSL 以非特权用户运行 `Allrun`，128 个声压值逐项一致。工作副本写入冲突的 `c0=90 m/s` 后，导出拒绝。首次 `Allrun` 在 Windows 挂载盘复制时间属性时失败，改为复制并核对输入字节后复验通过。 [离线证据](../../tmp/ff-cb03-acoustic-package-o4myjhuu/package.json) · [首次日志](../../tmp/ff-cb03-acoustic-package-i2obqwzc/allrun.log)

## 不可压缩参考压力

`physicalProblem.referencePressureRole gauge` 将 `referencePressure` 的 Pa 值除以 `referenceDensity` 的 kg/m³ 值，再写入原生运动学压力 `pRefValue`。`referencePressureCell` 指定压力基准单元，默认编号 0。当前用于无固定绝对压力边界的串行 `icoFoam` 和 `pimpleFoam`。原生 PISO 或 PIMPLE 块已有的值须与声明一致，缺项在封存的 `fvSolution` 中补齐。标准包保留原件，`Allrun` 在独立运行目录应用同一派生文件。 [生成与验证](../server/app/services/openfoam_pressure_reference.py) · [OpenFOAM 2406 压力方程](https://api.openfoam.com/2406/incompressible_2pimpleFoam_2pEqn_8H_source.html)

10 Pa 与 2 kg/m³ 生成 5 m²/s²。400 单元方腔的独立原生压力偏移误差约 `5 × 10⁻¹² m²/s²`，32 单元周期通道最大误差为 0。两套本机托管运行和离线包均与各自独立原生压力场逐字节一致。`pimpleFoam` 的认证 SeaweedFS 结果与文件系统一致，D 确认和封存 `fvSolution` 摘要通过。非法密度在保存前拒绝，原生 `pRefValue` 冲突在导出时拒绝。首轮系统目录选择同时纳入公开原件，修正为只封存派生版本后复验。 [工程证据汇总](../../tmp/FF-CB03-pressure-reference-20261123/validation.json)

真实 DeepSeek `deepseek-flash` 从实际归档派生的隔离证据文件选择 `read_text_file`，回读与原生压力场相同的 SHA-256。实际 `/v1/chat/completions` 两次 HTTP 200，工具结果已持久记录，模拟调用 0 次。 [真实模型回执](../../tmp/live-pressure-reference-48_015a9/live-result.json)

## 恒定转速的多参考系

将 `physicalProblem.coordinateFrame` 设为 `rotating`，并在 `rotatingFrame` 指定 `cellZone`、米制全局 `origin`、非零 `axis` 与 `angularVelocity { value; unit; }`。角速度支持 `rpm` 和 `rad/s`，`nonRotatingPatches` 可指定保持静止的原生边界。当前串行 `pimpleFoam` 将声明写入封存的 `constant/MRFProperties`。求解前用原生 `checkMesh` 检查单元区和静止边界，求解后核对日志加载的 MRF 区域。现有普通或 gzip 字典与声明一致时保留，冲突项拒绝运行与导出。 [输入生成与校验](../server/app/services/openfoam_rotation.py)

32 单元算例在两个存储以 30 rpm 运行，生成 π rad/s 角速度。0.1 s 和 0.2 s 的速度与压力同独立原生解逐字节一致，旋转壁面按 `Ω × (x − x₀)` 计算的最大误差为 `3.62 × 10⁻¹³ m/s`。静止端面变体在本机托管和离线包核验两端速度为零。零轴在保存前拒绝，原生角速度冲突在导出时拒绝。最初仅将单侧端面排除时原生求解器拒绝不平衡流量，改用两个相对端面后复验通过。 [实际验收](../../tmp/FF-CB03-rotation-20261123/validation.json)

## 运动坐标

设置 `coordinateFrame moving`，并让 `movingFrame.region` 引用带 `moving true`、`cellZone`、`motion.type linearMotion` 和米每秒速度的区域。平台将该区域用于串行 `pimpleFoam` 的 `dynamicMeshDict`，封存中记录实际区域、单元区和速度。结果要求原生日志证明单元区已启用线性运动。 [区域与原生设置](FF-CB06-regions.md)

`coreCells` 的 36 个单元及 98 个点在 0.1 s 沿 X 平移 2 mm。文件系统与认证 SeaweedFS 的实际位移和输入摘要一致，均确认 D。文件系统离线复算的网格点字节与托管相同。惯性坐标与运动块组合在保存前拒绝，指定未知区域时拒绝离线导出。 [文件系统及离线包](../../tmp/ff-cb06-motion-managed-8k_od8bu/porosity.json) · [认证对象存储](../../tmp/ff-cb06-motion-managed-ay2b1_s7/porosity.json)

## 变形坐标

设置 `coordinateFrame deforming`，在 `deformingFrame` 填写 `movingPatch`、`fixedPatch`、`freePatches`、米制位移分量 `amplitude` 和 `angularFrequency`（rad/s）。移动边界的 `0/U` 使用 `movingWallVelocity`，网格中的其余边界必须在声明中列出。串行 `pimpleFoam` 生成封存的 `dynamicMeshDict` 与 `0/pointDisplacement`，分别使用 `displacementLaplacian` 和 `oscillatingDisplacement`。算例的 `fvSolution` 需要配置 `cellDisplacement`、`cellDisplacementFinal` 及网格压力修正。 [输入生成](../server/app/services/openfoam_deformation.py)

独立 81 网格点算例将移动壁分别沿 Y 移动约 9.589 mm 和 16.829 mm，固定端位置保持，内部点产生连续位移。最大壁面误差约 `1.58 × 10⁻¹³ m`。本机托管与标准包离线复算的两个时刻网格点均与独立原生逐字节一致。实际公开保存拒绝零频率，导出拒绝手工点位移字典与声明冲突。 [独立原生](../../tmp/FF-CB03-deformation-20261123/native.json) · [文件系统与离线包](../../tmp/ff-c10-udf-deformation-x09t6rkn/deformation.json)

PostgreSQL 与认证 SeaweedFS 确认 B–D。两个生成文件和两个输出时刻的网格点摘要与文件系统同次参数验收相同。首批独立算例的自由边界误用 `calculated`，随后原生要求补齐 `cellDisplacementFinal`。测试输入调整为零梯度位移和完整原生求解器设置后通过。 [认证对象存储](../../tmp/ff-c10-udf-deformation-d_egh33h/deformation.json) · [临时 PostgreSQL 清理](../../tmp/terminal-agent-postgres-g9pb79fm/cleanup.json)

## 连续介质守恒方程

在 `governingModels.continuum` 选择 `model continuum`，通过 `conservation` 声明实际求解的方程。串行不可压缩 `icoFoam`、`simpleFoam`、`pimpleFoam`、`interFoam` 对应质量和动量，`rhoCentralFoam` 另有能量，`laplacianFoam` 求解能量。平台核对原生应用和运行日志，归档真正求解的方程名。 [原生方程映射](../server/app/services/openfoam_equations.py)

理想气体激波管在本机托管和标准包以质量、动量、能量三方程求解，通过末态四个场的独立核对。将能量改成电荷后，标准包在导出前给出明确的方程不匹配信息。 [实际求解和负例](../../tmp/ff-c07-compressible-ykdoob6g/compressible.json)

## 压力力系数

对 `pressureForce` 边界积分指定 `normalization physicalProblem`。后处理按 `referenceDensity`、`referenceVelocity` 的幅值和 `referenceArea` 形成参考力 `0.5·ρ·U²·A`。作用力先用同一物理块的参考压力及密度转换为 N，再输出三个无量纲力系数。报表可引用诸如 `upper.pCoefficient[0]@1000` 的指标。归档中保留输入和参考力，额外声明的密度或压力要与物理块一致。 [后处理实现](../server/app/services/postprocess_executor.py)

12,225 单元的真实 PitzDaily 使用速度向量 `(3 4 0)` m/s、密度 1.2 kg/m³、面积 0.25 m²，参考力为 3.75 N。文件系统与认证 SeaweedFS 的无量纲压力力逐项相同，与各自同次原生压力力的相对误差约为 `2.64 × 10⁻⁸`。两套存储均确认 E，并生成包含有量纲力和无量纲力系数的报告。 [原生与报告证据](../../tmp/FF-C14B-pitz-forces-20261123/normalization-validation.json)

将积分模式设为 `pressureMoment`，以 `momentCenter` 指定米制全局原点，可按同一参考压力、密度计算各分量的压力力矩。选择 `normalization physicalProblem` 时，用 `referenceLength` 把参考力乘以米制参考长度，所得尺度用于输出三个无量纲力矩系数。报告支持同时引用 `wallMoment.p[2]@300` 和 `wallMoment.pCoefficient[2]@300`。 [积分和归档](../server/app/services/postprocess_executor.py)

PitzDaily 使用 0.29 m 参考长度，得到 1.0875 N·m 的尺度。托管结果为 `−4.200003845 × 10⁻⁶ N·m`，与同网格独立原生的相对差约 `6.48 × 10⁻⁸`。文件系统和认证 SeaweedFS 均确认 E，通过合力、力系数、力矩和力矩系数共六项比较。首轮使用不同坐标精度的网格作为独立参考，力矩超过原容差。用同一网格的坐标文件独立复算后，两边的压力摘要相同，报告按原容差通过。 [实算及报告验收](../../tmp/FF-C14B-pitz-forces-20261123/moment-validation.json)

## T02 验收

在 caseDict 面板的结构化编辑中选择 `physicalProblem`，可编辑坐标、重力、参考量和控制模型。选择另一种控制模型时，旧模型的专属参数从当前草稿移除。版本冲突保留草稿并提供服务器版本。Edge 在 1440、900、640 像素宽度核验保存、刷新与键盘操作。绕过浏览器直接发起的跨用户读取和写入被实际接口拒绝，原修订未变化。 [浏览器及原生回执](../../tmp/ff-cb03-physical-fields-utjd4n58/edge.json) · [权限回执](../../tmp/FF-CB03-physical-ui-20261123/permissions.json)

2026-11-23，FF-CB03 已按原 T02 验收。浏览器保存的旋转坐标、层流与守恒方程由真实 `pimpleFoam` 执行，压力和速度同独立原生结果逐字节一致。流体、固体、传热、电磁、声学和耦合的实际输入与数值已归入支持矩阵。高级求解器行为和新接口继续按 FF-C01、FF-C18、FF-CB07 的原计划验收。 [本项验收汇总](../../tmp/FF-CB03-physical-ui-20261123/t02-validation.json) · [物理支持矩阵](FF-C01-physical-models.md#实际支持矩阵)

## FF-C18 T04 验收

`buoyantBoussinesqSimpleFoam` 可声明 `continuum` 的质量、动量和能量守恒。原生日志必须记录 `p_rgh`、速度分量与 `T` 的方程求解，遗漏能量方程的声明被拒绝。实际热房间算例将局部坐标中的重力转换为全局 `(0, −9.81, 0) m/s²`，同时将 36.85°C 设为 310 K 浮力参考温度。文件系统和认证对象存储确认 D，测得峰值速度约 0.543640 m/s，封存压力、速度和温度同独立离线复算逐字节一致。 [OpenFOAM 2406 传热求解器](https://api.openfoam.com/2406/group__grpHeatTransferSolvers.html) · [文件系统](../../tmp/ff-cb03-openfoam-gravity-a1ciivyh/gravity.json) · [认证对象存储](../../tmp/ff-cb03-openfoam-gravity-mud9lmpw/gravity.json)

FF-C18 按 T04 汇总坐标、重力、参考量与流、固、热、电磁、声学的后端输入、定量对照和工作台回执。浮力复验中真实模型和模拟均为 0 次。此前对封存参考压力的工具回读由真实 `deepseek-flash` 完成，2 次 HTTP 200。 [T04 验收](../../tmp/FF-CB03-physical-schema-20261122/c18-t04-validation.json) · [真实模型回执](../../tmp/live-pressure-reference-48_015a9/live-result.json)
