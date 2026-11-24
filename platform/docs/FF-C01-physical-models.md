# FF-C01 原生物理控制模型

## OpenFOAM 不可压缩流动

在 `physicalProblem.governingModels.fluid` 用 `turbulenceModel laminar` 或 `turbulenceModel kEpsilon` 选择不可压缩单相控制模型。`kEpsilon` 可用 `turbulenceCoefficients.Cmu` 指定正数系数。串行求解及标准 OpenFOAM ZIP 生成同一 `turbulenceProperties`。存在原生字典时保留与声明一致的原件，`model` 和 `RASModel` 同时存在且取值不同时拒绝导出和运行。 [输入生成与原生日志校验](../server/app/services/openfoam_fluid_model.py)

实际 32 单元 `pimpleFoam` 的层流与 `Cmu=0.06` 标准 k–ε 在文件系统和 PostgreSQL／认证 SeaweedFS 分别确认 B–D。0.1 s 和 0.2 s 的速度、压力、适用的湍流字段及生成字典在双存储逐字节一致，结果同独立原生和本机 `Allrun` 离线复算一致。 [验收回执](../../tmp/FF-C01-fluid-model-20261123/validation.json)

## 理想气体与双相 VOF

`physicalProblem.governingModels.fluid` 使用 `thermophysicalModel perfectGas`、`compressibility compressible` 与 `material` 引用已确认气体。`rhoCentralFoam` 的 `thermophysicalProperties` 使用材料的摩尔质量、定压比热、生成焓、动力黏度和普朗特数。当前热物性为纯物质、恒定输运、恒定比热及理想气体状态方程。公开原生文件存在时校验材料和模型，缺少文件时生成私有输入。 [流体材料实现](../server/app/services/openfoam_fluid_thermophysics.py)

100 单元激波管从声明换算出 `28.96 kg/kmol`、`1004.5 J/(kg·K)` 与 `2544000 J/kg`。两套存储确认 D，0.007 s 的压力、速度、密度和温度与独立原生逐字节一致，解析压力相对 L2 为 0.0138951。本机标准包复算、匹配原件保留和状态方程冲突拦截通过。 [文件系统与离线包](../../tmp/ff-c07-compressible-fko7w7fg/compressible.json) · [认证对象存储](../../tmp/ff-c07-compressible-rh49i8ga/compressible.json)

`multiphase VOF` 配合 `phaseOrder`、`phaseMaterials` 和 `surfaceTension`，按明确的相顺序将已确认材料写入 `interFoam` 的 `transportProperties`。水和空气的密度为 1000 与 1 kg/m³，运动黏度为 `1 × 10⁻⁶` 与 `1.48 × 10⁻⁵ m²/s`，表面张力为 0.07 N/m。实际文件系统工作流确认 E、生成体积分报告，末态水相比例与独立原生结果一致。 [文件系统体积分及报告](../../tmp/ff-c14b-dam-break-iooovn2r/dam-break.json)

认证 SeaweedFS 的四写出和二十写出时刻两次复验均完成 E 和报告。两次分别使用独立原生的相同步长与写出设置，水相体积分相对误差均为 0。20 时刻首轮的来源校验超时保留，修复超时被误标为成果过期后，单独复跑于 23.06 秒确认 E。 [四时刻](../../tmp/ff-c14b-dam-break-cclfy4hh/dam-break.json) · [二十时刻](../../tmp/ff-c14b-dam-break-giv00dlu/dam-break.json) · [首次超时](../../tmp/ff-c14b-dam-break-jre1p_ca/dam-break.json)

## CalculiX 小应变线弹性

在 `physicalProblem.governingModels.solidMechanics` 指定 `materialLaw linearElastic`、`kinematics smallStrain` 和 `dynamics static`。`material` 从已确认材料取得弹性模量和泊松比，`nativeMaterial` 将材料绑定到计算输入中的实体截面和 `*MATERIAL`。平台在同一封存 CalculiX 输入中应用载荷及各向同性弹性，记录相同的原生输入摘要。现有原生弹性与确认材料不符，或材料使用本流程不支持的非线性模型时，提交返回明确的错误。 [CalculiX 绑定和转换](../server/app/services/calculix_elasticity.py)

实际 1 m 立方体测试使用 5 mm 固定端位移与 10 N 拉力。弹性模量由 1 kPa 增至 2 kPa，加载端位移由 15 mm 降至 10 mm，轴向应力均为 10 Pa，支座反力均为 −10 N。文件系统和认证 SeaweedFS 的实际原生结果、封存输入摘要均与独立计算一致。三项输入故障在文件系统通过拦截，已确认载荷及生成节点组通过旧流程回归。 [验收回执](../../tmp/FF-C01-calculix-elasticity-20261123/validation.json)

活跃载荷的 `activeMaterials` 可为此线弹性模型选择一个已确认材料，并保持配置中的原生 `*MATERIAL` 名称。两个不同载荷携带相同力和位移。切换活跃载荷后，双存储分别封存 1 kPa 与 2 kPa，15 mm 和 10 mm 的结果与独立原生一致。实际归档记录载荷名称、选择的材料及最终输入摘要。多个活跃材料或未确认材料在派发前被拒绝。 [文件系统](../../tmp/ff-c01-calculix-elasticity-ltemtrlt/elasticity.json) · [认证对象存储](../../tmp/ff-c01-calculix-elasticity-pnhddvik/elasticity.json)

## 材料导热

在 `physicalProblem.governingModels.thermal` 选择 `transport conduction`，以 `material` 引用已确认材料。托管串行 `laplacianFoam` 将密度、比热和导热率换算成 SI，再按 `DT = k/(ρ·cp)` 得到热扩散系数。现有普通或 gzip 的 `transportProperties.DT` 须与计算值一致，缺少原生字典时生成私有输入。标准包使用同一字典。 [材料导热生成](../server/app/services/openfoam_conduction_material.py)

32 单元实际计算把 `2.5 g/cm³` 和 `1 kJ/(kg·K)` 换成 2500 kg/m³ 与 1000 J/(kg·K)。导热率 250、500 W/(m·K) 分别得到 `0.0001`、`0.0002 m²/s`，0.4 秒最高温为 300.509942059、301.015793873 K。文件系统、认证 SeaweedFS 和本地离线包的两个时刻与独立原生场逐字节一致。未确认材料和原生 DT 冲突拒绝导出。首轮测试算例的二维声明被原生网格质检拒绝，改用实际三维声明后复验。 [实际工程结果](../../tmp/FF-C01-conduction-material-20261123/validation.json)

`activeLoadCase` 的 `activeMaterials` 可从现有材料库选择唯一确认材料。保留 `coupon` 和 `variant` 两条记录，先导出 `low` 载荷并核验 `DT=0.0001 m²/s`，再切换到 `high` 载荷，实际封存 `DT=0.0002 m²/s` 并确认 B–D。双存储和离线程序的温度场一致，未确认材料、多个活跃材料与原生值冲突均被拦截。 [文件系统及离线](../../tmp/ff-c10-udf-conduction-material-lvrgie4h/conduction-material.json) · [认证对象存储](../../tmp/ff-c10-udf-conduction-material-qdupicum/conduction-material.json)

## 共轭传热的区域材料

在 `governingModels.thermal` 选择 `transport conjugate` 与 `materialSource regions`。`chtMultiRegionFoam` 读取 `regions` 绑定的已确认气体、液体和固体材料，按区域生成或者校验原生 `thermophysicalProperties`。现有支持组合分别是理想气体、恒定密度流体和各向同性导热固体。生成文件绑定到求解发布，标准包使用同一批输入。 [区域材料生成](../server/app/services/openfoam_cht_materials.py)

9,000 单元的五区域算例生成五份热物性字典，实际跨界面求解确认 D。0.005 s 的加热体最高温度为 306.07834 K，全部五区的温度字段在文件系统、认证 SeaweedFS 和本机离线复算逐字节相同。保留匹配的原生材料文件，原生导热率冲突拒绝导出。 [文件系统及离线](../../tmp/ff-cb06-cht-managed-fslb1q3p/cht.json) · [认证对象存储](../../tmp/ff-cb06-cht-managed-c1mp0q5i/cht.json)

选择 `allRegions` 载荷时，`activeMaterials` 必须覆盖所有区域绑定。`air`、`water` 和 `metal` 三项覆盖五个区域，双存储的五份封存热物性和温度场一致；缺少固体材料时标准包拒绝导出。 [活动载荷文件系统实测](../../tmp/ff-cb06-cht-managed-f_bjs98k/cht.json) · [活动载荷认证对象存储实测](../../tmp/ff-cb06-cht-managed-mn60_dye/cht.json)

## Elmer 电热耦合

在 `physicalProblem.coupledPhysics` 启用 `electromagnetics` 和 `heatTransfer`。令 `physicalProblem.governingModels.electromagnetic.coupling` 为 `jouleHeating`，通过原生 Elmer 求解提交稳态 SIF。控制模型校验同一体区域的原生方程先运行 `StatCurrentSolve`，后运行 `HeatSolve`。封存副本为静态电流启用 `Calculate Joule Heating`，并让热求解的对应体源接收 `Joule Heat`。已有原生设置须与实际声明相符。归档回执保留实际体、两个求解器及封存 SIF 的摘要。 [配置生成](../server/app/services/elmer_joule.py) · [原生静态电流源码](https://raw.githubusercontent.com/ElmerCSC/elmerfem/release-26.2.1/fem/src/modules/StatCurrentSolve.F90)

1 m 导体两端分别施加 0 V、5 V，并固定温度 300 K。电导率为 2 S/m，热导率为 1 W/(m·K)。真实静态电流计算产生 50 W/m³ 焦耳热。未施加焦耳热源时全域为 300 K。选中耦合后中点达到 306.25 K，12 个节点的温度和独立解析解最大差为 `1.14 × 10⁻¹³ K`。实际托管作业按原生范围验证停止，保留工作副本 SIF。已有原生关闭值与启用声明冲突、求解器次序倒置均在派发前拒绝。 [独立原生](../../tmp/FF-C01-elmer-joule-20261123/native.json) · [文件系统](../../tmp/ff-c01-elmer-joule-qf09j0ii/joule.json)

Edge 从 Native 作业历史通过键盘读取确切发布，显示电流到热传导的求解次序，选中完整 SIF 摘要，并下载对应归档的真实 VTU。三种视口宽度通过。 [Edge](../../tmp/cb08-h18-edge-joule-uioqmgjj/browser.json)

PostgreSQL 与认证 SeaweedFS 复验了开启和关闭的两组实际求解。温度、电势和焦耳热各项数值与文件系统一致，两种原生冲突同样在派发前拒绝。 [认证对象存储](../../tmp/ff-c01-elmer-joule-fk67z5kw/joule.json) · [临时 PostgreSQL 清理](../../tmp/terminal-agent-postgres-u_8i86bw/cleanup.json)

## Elmer 静电介质

在 `physicalProblem.governingModels.electromagnetic` 指定 `formulation electrostatic`，通过 `material` 引用已确认的各向同性介质。当前原生输入使用一个 Cartesian 3D 介质体与 `StatElecSolve / StatElecSolver`。平台在私有 SIF 生成材料的 `Relative Permittivity`、电场及储能输出设置，并允许由已确认 `voltage` 边界生成电势。成果保留实际原生体、材料、求解器及封存 SIF 摘要。 [Elmer 26.2 静电实现](https://github.com/ElmerCSC/elmerfem/blob/release-26.2.1/fem/src/modules/StatElecSolve.F90) · [材料和求解验证](../server/app/services/elmer_electrostatic.py)

1 m 介质两端为 0 V、5 V。相对介电常数从 2 增至 4 时，电场幅值均为 5 V/m，原生积分储能从 `2.21354695 × 10⁻¹⁰ J` 加倍至 `4.42709391 × 10⁻¹⁰ J`，符合均匀介质的解析值。文件系统和认证 SeaweedFS 的电场、能量及封存 SIF 摘要一致。Edge 在三个宽度读取材料并下载所选版本的 VTU。已实测未确认材料、电流边界、原生介电常数冲突的拒绝路径，既有焦耳热实算回归通过。 [实际验收](../../tmp/FF-C01-elmer-electrostatic-20261123/validation.json)

同一算例和 SIF 保留 `dielectric` 与 `variant`。切换 `staticField` 和 `variantField` 载荷时，封存回执记录选择的介质和载荷，两套存储的 SIF 摘要、电场和储能一致。两次原生 VTU 的归档摘要分别记录。 [活动载荷文件系统实测](../../tmp/ff-c01-elmer-electrostatic-pgs8ucu7/electrostatic.json) · [活动载荷认证对象存储实测](../../tmp/ff-c01-elmer-electrostatic-xshlqfwg/electrostatic.json)

## 实际支持矩阵

| 学科 | 当前已跑通的声明 | 生成或核对的原生输入 | 实际测量 |
| --- | --- | --- | --- |
| 流体 | 不可压层流和 k–ε、可压缩理想气体、双相 VOF | `turbulenceProperties`、`thermophysicalProperties`、`transportProperties` | 速度和压力、湍流黏度、激波四字段、水相体积分 |
| 固体力学 | 静态小应变、各向同性线弹性 | CalculiX `*MATERIAL`、`*ELASTIC`、实体截面以及确认载荷 | 端面位移、应力和反力 |
| 传热 | 单材料导热、区域共轭传热 | `laplacianFoam` 的 `DT` 和确认的初值与边界，多区域热物性 | 两个扩散系数的逐单元温度以及五区温度 |
| 电磁 | 各向同性静电介质 | Elmer `StatElecSolve`、相对介电常数和确认电压 | 电场、积分储能 |
| 声学 | 压力波、介质声速和密度 | `acousticFoam` 的 `c0`、`rho` 及确认边界 | 末态压力，出口阻抗见 FF-CB08 |
| 耦合 | 稳态电流与热传导、单向流体压力到结构载荷 | Elmer 焦耳热源，版本化压力合力与 CalculiX 节点载荷 | 导体中点温升，弹性位移、应力和反力 |

压力、速度和温度的归档证据可按求解发布读取。 [声学输入与对照](FF-CB03-physical-problem.md#压力波声学介质) · [单向压力传递](FF-CB07-interfaces.md#openfoam-压力到-calculix-的单向载荷)

## 其余原生验证

- OpenFOAM 已验证不可压、湍流、热、多相、可压缩与声学。 [原生求解矩阵](FF-C07-openfoam-matrix.md)
- 实际 CalculiX 通过模态和有阻尼、无阻尼谐响应，原生结果按发布版本读取。 [结构分析](FF-C15-analysis-modes.md)
- 实际 Elmer 电压与电流边界，以及 OpenFOAM 出口声阻抗，均有定量对照。 [边界条件](FF-CB08-boundaries-loads-initials.md)
- 共轭热、机械接触和 Overset 接口使用实际原生求解。 [工程接口](FF-CB07-interfaces.md)

## T04 验收

`laplacianFoam` 选用活动载荷中的 `variant` 后，原生作业在写出 0.2 s 时实际取消。停止核验后从该时刻启动新尝试，保留封存的 `DT=0.0002 m²/s`，续算到 0.4 s 并确认 D。32 个单元与相同步长、从零独立运行的最大温差为 `9.9999 × 10⁻¹⁰ K`。首轮脚本在取消收尾写入停止证明前读取状态，修正为等待恢复入口可用后通过。 [真实取消与续算](../../tmp/ff-c10-udf-conduction-material-y1z7jn4u/conduction-material.json) · [首轮记录](../../tmp/ff-c10-udf-conduction-material-t2_jbhl8/conduction-material.json)

FF-C01 原 T04 已验收，包含六类问题的声明、各自真求解、定量对照、输入和输出归档，以及上述取消和续算。材料与跨物理接口的专项范围继续按 FF-A07、FF-C18 和 FF-CB07 交付。本组工程回执真实模型 0 次，模拟 0 次。 [验收矩阵](../../tmp/FF-C01-conduction-material-20261123/t04-validation.json) · [台账](../../tmp/stage-ledger-evidence-20261123-ff-c01.json)
