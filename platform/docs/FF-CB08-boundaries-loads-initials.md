# FF-CB08 边界、初值与载荷

`boundariesLoadsInitials` 的命名边界支持角色、物理类型、单位、网格边界名称和确认状态。`timeProfile` 支持 `constant`、`table`、`function`（线性）、`harmonic`、`ramp` 和 `pulse`。时间用秒表示，谐波频率用 Hz，初相位用 rad。`profileData` 的表时间必须严格递增，脉冲需要显式给出上升和下降时长。未确认的草稿允许保存并在配置检查中提示。 [配置校验](../server/app/services/case_boundary_inputs.py) · [OpenFOAM 时间函数](https://doc.openfoam.com/2306/fundamentals/input-types/Function1/) · [OpenFOAM 边界配置](https://doc.openfoam.com/2212/tools/processing/boundary-conditions/rtm/derived/general/uniformFixedValue/)

单区域托管 `icoFoam` 和 `pimpleFoam` 从 `initialConditions { initialization uniform; fields { ... } }` 及已确认网格读取速度 `U` 和运动学压力 `p`。两个 Boussinesq 求解器支持速度与温度，稳态求解使用常量边界。输入采用 OpenFOAM 的物理量纲，Pa 和 kPa 按显式 `referenceDensity` 转成运动学压力，摄氏温度换算成 K。缺少原生初值场时，作业封存目录内按真实网格生成，普通物理边界需给出显式条件。已有原生文件的初值、量纲及已声明边界必须与 `caseDict` 相符。 [原生字段生成](../server/app/services/openfoam_boundary_inputs.py) · [输入封存](../server/app/services/solve_handler.py)

命名 `activeLoadCase` 选定参与计算的边界。托管求解验证当前载荷和参与边界，用原生 `zeroGradient`、`noSlip`、`fixedValue` 或 `uniformFixedValue`，以及网格自带的周期、空面和对称条件。配置校验识别材料和物理引用，当前执行支持流体和传热边界载荷。 [载荷选择实现](../server/app/services/openfoam_boundary_inputs.py)

2026-11-22，实际 SQLite 文件系统以 36 单元周期流道验证表和谐波速度。`0.1 s` 壁面速度均为 `0.5 m/s`，单元最大速度分别为 `0.354956679963 m/s` 与 `0.390751284357 m/s`，逐项与独立 `icoFoam` 基准吻合。`0/U` 在求解尝试中封存，公开工作副本无该文件，两个流程都确认了 D。非法单位、未定义载荷引用在写入前返回 422，原修订可读。参与载荷之外的未确认边界保留提示。 [时间表托管回执](../../tmp/ff-cb08-boundary-profile-8gnu43ib/boundary-profile.json) · [谐波托管回执](../../tmp/ff-cb08-boundary-profile-76czutey/boundary-profile.json)

时间表在 PostgreSQL 加认证 SeaweedFS 上得到相同速度和 `0/U` 封存摘要。其余四种速度曲线也与独立原生算例一致，通过 D。斜坡算例同时生成 `U` 和 `p`，两个初值文件都只出现在私有封存目录。实际热流算例从 `36.85 °C`、`26.85 °C` 生成 `310 K`、`300 K`，256 个单元温度与独立浮力求解一致，终态范围 `300.287–301.057 K`。 [认证对象存储](../../tmp/ff-cb08-boundary-profile-dejsslyp/boundary-profile.json) · [双场初值](../../tmp/ff-cb08-boundary-profile-yh7d_r_z/boundary-profile.json) · [真实温度场](../../tmp/ff-cb08-temperature-u8o7nw7r/temperature.json)

首次负例暴露了确定性准备错误停留在 `ready`。现在求解步骤记录 `blocked` 和具体原因，工作流进入失败终态，保留已确认的 C 和全部已有文件。已实测被选中边界未确认、原生场冲突两个路径，均无新求解尝试或成果发布。认证 SeaweedFS 也通过了未确认边界的拒绝验收。 [负例与汇总](../../tmp/FF-CB08-boundaries-20261122/validation.json) · [认证存储拒绝](../../tmp/ff-cb08-boundary-profile-119wdkag/boundary-profile.json)

显式原生续算入口可从取消作业的原生检查点恢复原时间轴。这里的 `initialConditions` 指定新求解的初始场。 [显式续算](openfoam-checkpoint-resume.md)

## 映射初值原生试算

2026-11-23，OpenFOAM 2406 的 `mapFieldsPar` 以单进程 `cellVolumeWeight` 将已实算的 64 单元源结果从 `t=1` 映射到独立 512 单元目标的 `t=0`。原生输出覆盖整个单位立方体，生成 512 个内部值，其中 312.5、337.5、362.5、387.5 K 各 128 个。随后用映射后的目标目录实际运行 `laplacianFoam`，`t=1` 的体积平均温度为 350 K。该算例用于对照托管映射。 [映射日志](../../tmp/FF-CB08-mapped-20261123/native-map.log) · [目标求解日志](../../tmp/FF-CB08-mapped-20261123/native-solve.log) · [数值回执](../../tmp/FF-CB08-mapped-20261123/native-mapped-heat.json) · [OpenFOAM 映射指南](https://www.openfoam.com/documentation/user-guide/4-mesh-generation-and-conversion/4.6-mapping-fields-between-different-geometries)

## 单区域导热初值与边界

`laplacianFoam` 支持声明 `T` 均匀初值、固定温度与 `zeroGradient` 边界。两套存储实际生成相同的封存 `0/T`，初温为 320 K，壁温分别为 300 K 和 400 K。64 单元求解的四组温度为 312.5、337.5、362.5、387.5 K，各 16 个，解析最大误差为 0 K，均通过 D。工作副本中的冲突 `0/T` 保留原字节，求解步骤阻止派发。 [文件系统实算](../../tmp/ff-cb08-conduction-4uut6k0w/conduction.json) · [认证对象存储实算](../../tmp/ff-cb08-conduction-cc1s5s2q/conduction.json) · [初值冲突](../../tmp/ff-cb08-conduction-1yofzsw6/conduction.json)

## 同网格历史场

`initialConditions` 设为 `initialization restart`，在 `source` 提供当前算例已发布的 `attemptId` 和原生写出 `time`，可用 `fields (T)` 等列表显式选场。确认当前边界，并将生成目标字段的位置留给作业封存。源字段须采用单区域静态网格上的 OpenFOAM ASCII 数值，支持 gzip 归档。`restart` 核对点、面、owner、neighbour 和边界文件的原始摘要。新作业按当前运行配置使用源内部值及当前边界，从 `t=0` 开始。`GET /cases/{case_id}/solve/results` 的 `initial_source` 返回来源发布、时刻及字段摘要。 [源字段验证](../server/app/services/openfoam_initial_source.py)

两种存储均逐单元复制 64 单元 `t=1` 温度，确认新壁温 330 K、430 K 后实际求解，解析最大误差为 0 K。先前发布保持可读。不同拓扑、未写出的时刻和不可用来源均在启动新求解前拦截。 [文件系统](../../tmp/ff-cb08-conduction-6vuby2pg/conduction.json) · [认证对象存储](../../tmp/ff-cb08-conduction-xlpubewb/conduction.json) · [不同网格](../../tmp/ff-cb08-conduction-at4ngjft/conduction.json) · [未写出时刻](../../tmp/ff-cb08-conduction-p050eu35/conduction.json) · [不可用来源](../../tmp/ff-cb08-conduction-cw310b2s/conduction.json)

## 异网格托管初值

将同一声明的 `initialization` 设为 `mapped`。当前实现使用 `cellCenters`，为每个目标单元中心选择覆盖该点的源单元，并按原生单元编号生成 `0/<field>`。源与目标网格来自各自封存发布，映射结果使用当前边界。结果接口记录两个网格发布和覆盖单元数。在源域外检测到目标中心时，求解步骤在派发前给出未覆盖单元数。 [映射实现](../server/app/services/openfoam_initial_mapping.py)

文件系统与认证对象存储通过 64→512 单元应用及实际导热，封存场与独立原生映射在 512 个单元逐项一致，两边摘要相同。对象验收在移走本机源发布缓存后读取归档，源成果仍可回查。移出源域的实际 512 单元网格在准备时全部拦截。 [文件系统映射](../../tmp/ff-cb08-conduction-iqc8ssbu/conduction.json) · [认证对象存储映射](../../tmp/ff-cb08-conduction-b9zf6orp/conduction.json) · [域外拒绝](../../tmp/ff-cb08-conduction-5simrym0/conduction.json)

## 解析公式与 CSV 初值

`initialization analytical` 的字段可用 `model linear`，声明 `axis x|y|z`、`origin`、`value`、`slope` 和 `unit`。原点单位为 m，斜率按字段单位每米。平台在确认网格的实际单元中心计算字段，以原生单元编号排序后生成并封存初值。摄氏温度基准转换为 K，温差斜率保持每米温差。 [解析字段配置](../server/app/services/openfoam_boundary_inputs.py)

`initialization fromMeasurement` 读取同一运行目录 `system` 下的 UTF-8 CSV。`source.path` 写相对运行目录的路径，`coordinateUnit` 可用 `m` 或 `mm`，`maxDistance` 以米表示。CSV 包含 `x,y,z` 和字段所引用的 `columns`，字段声明输入 `unit`。最近采样点超出距离时，工作流在派发前报告未覆盖单元数。求解结果的 `initial_source` 保留网格发布、样本量、源 CSV 摘要、单位和最大实测距离。 [CSV 字段验证](../server/app/services/openfoam_initial_measurement.py)

文件系统实测 64 单元瞬态导热。用 `300+100x` 生成的初值与原生结果逐单元一致，解析最大误差为 0 K。CSV 验收使用同一解析场的 64 个带坐标验证样本，封存原件和私有 `0/T`，原生解的最大误差也为 0 K。实际负例拒绝低于绝对零度的解析字段，以及移开后的全部 64 个 CSV 样本。 [解析初值](../../tmp/ff-cb08-conduction-gdcsvgy4/conduction.json) · [CSV 初值](../../tmp/ff-cb08-conduction-pp81bg46/conduction.json) · [非物理值](../../tmp/ff-cb08-conduction-80tyypo9/conduction.json) · [CSV 覆盖负例](../../tmp/ff-cb08-conduction-x7hw3ayh/conduction.json)

同一解析算例在 PostgreSQL 和认证 SeaweedFS 验收，64 单元最终温度与解析场逐单元相同，生成初值的封存摘要与文件系统一致。 [对象存储解析初值](../../tmp/ff-cb08-conduction-nr2nll1z/conduction.json)

同一 CSV 通过 PostgreSQL 与认证 SeaweedFS。输入坐标从 mm 转成 m，摄氏列转换成 K。64 个单元中心全部匹配，最远距离为 0 m，生成 `0/T`、输入原件摘要以及两库的原生解一致。 [对象存储 CSV 初值](../../tmp/ff-cb08-conduction-ouc7cqjj/conduction.json)

## 固定热流

`laplacianFoam` 的 `wall` 或 `heatSource` 边界可声明 `physicalType heatFlux`。用 `value` 与 `unit` 提供向内的固定热流密度，并用 `material` 引用已确认的材料热导率。对于正向内热流，生成的原生外法向温度梯度为热流除以热导率。字段写入封存的 `0/T`。 [热流配置实现](../server/app/services/openfoam_heat_flux.py) · [OpenFOAM fixedGradient](https://doc.openfoam.com/2306/tools/processing/boundary-conditions/rtm/derived/general/fixedGradient/)

文件系统与 PostgreSQL 加认证 SeaweedFS 均将 200 W/m² 和 2 W/(m·K) 生成 100 K/m 梯度。实际求解的 64 个单元与解析温度逐项一致，重建壁温 400 K，两套存储的封存初值摘要相同，D 已确认。未确认材料在原生求解派发前被拒绝。 [文件系统](../../tmp/ff-cb08-conduction-7a2_vmg4/conduction.json) · [认证对象存储](../../tmp/ff-cb08-conduction-ohitbtb8/conduction.json) · [未确认材料](../../tmp/ff-cb08-conduction-l8ptrcng/conduction.json)

## 对流换热

单区域 `laplacianFoam` 的 `wall` 或 `heatSource` 可声明 `physicalType convection`。`value` 是正的换热系数，`unit` 使用 `W/m2/K`。`ambientTemperature` 记录环境的 `value` 和 `unit`，支持 K 和 `degC`。`material` 引用已确认热导率，热扩散系数取自原生 `transportProperties.DT`。串行作业加载安装版本的 `libthermoTools.so`，生成原生 `externalWallHeatFluxTemperature` 的 `coefficient` 模式，`0/T` 保留在封存目录。 [原生条件文档](https://doc.openfoam.com/2312/tools/processing/boundary-conditions/rtm/derived/thermal/externalWallHeatFluxTemperature/) · [边界渲染](../server/app/services/openfoam_heat_flux.py)

单位厚度的固体左壁 300 K，右边环境 400 K，换热系数 8 W/(m²·K)，材料导热率 2 W/(m·K)。解析右壁为 380 K，向内热流为 160 W/m²。实际 512 个单元的温度与解析值及独立原生执行完全一致。文件系统和认证 SeaweedFS 的封存初值、末态温度摘要一致，均确认 D。负的绝对环境温度在保存前拒绝，未确认材料阻止派发。既有固定热流算例复验通过。 [原生求解日志](../../tmp/FF-CB08-convection-20261123/laplacianFoam-lib.log) · [文件系统](../../tmp/ff-cb08-conduction-m_rik2v6/conduction.json) · [认证对象存储](../../tmp/ff-cb08-conduction-dayvms9k/conduction.json) · [材料拦截](../../tmp/ff-cb08-conduction-o76_sp46/conduction.json) · [固定热流回归](../../tmp/ff-cb08-conduction-fl8adn80/conduction.json)

## CalculiX 位移与总力

在 `boundariesLoadsInitials` 中可以为原生 `*NSET` 声明 `support/displacement` 与 `loadSurface/force`。`patch` 采用原生节点组名。位移支持 m、mm，力支持 N、kN，托管输入按 m 和 N 写入。力向量表示节点组总力，等分到实际成员节点。求解选择 `activeLoadCase` 的已确认恒定载荷，并在封存的单个 `*STATIC` 步里生成 `*BOUNDARY` 与 `*CLOAD`。原始 `.inp` 保持原字节。 [编译和核验](../server/app/services/calculix_boundary_inputs.py) · [原生作业](../server/app/services/calculix_worker.py)

单位立方体实算支座位移 5 mm 和端面总力 10 N。四个受力节点各分得 2.5 N，四个支座合反力为 −10 N，端面位移 15 mm，八个应力积分点的轴向应力均为 10 Pa。文件系统和 PostgreSQL 加认证 SeaweedFS 的封存输入、DAT 摘要和独立原生结果相符。以 `*NSET,GENERATE` 定义同一组节点的文件系统运行，DAT 摘要仍相同。缺少命名节点组阻止派发，未选中的草稿保持可编辑。真实机械接触的独立回归通过。 [独立原生](../../tmp/FF-CB08-calculix-loads-20261123/reference.json) · [文件系统](../../tmp/ff-cb08-calculix-loads-lktb4qh2/mechanical.json) · [认证对象存储](../../tmp/ff-cb08-calculix-loads-jc3sf0z5/mechanical.json) · [生成式节点组](../../tmp/ff-cb08-calculix-loads-fn3ls4tt/mechanical.json) · [接触回归](../../tmp/ff-cb07-calculix-contact-t_j05j0g/calculix-contact.json)

## Elmer 直流电压与总电流

原生稳态 `StatCurrentSolve` 的 `electrode` 边界选用 `physicalType voltage` 或 `current`。`patch` 填写网格中的数字边界标签。已选载荷使用 V、A 或 mA，电流正值表示向内。平台从网格的外表面三角形或平面四边形计算面积，按原生坐标缩放换算到 m²，并把总电流除以面积写成 `Current Density`。电压写成原生 `Potential`。来源追溯包含标签、面积、SI 值、原生值与封存 SIF 摘要。 [Elmer 原生边界项](https://raw.githubusercontent.com/ElmerCSC/elmerfem/release-26.2.1/fem/src/modules/StatCurrentSolve.F90) · [电极编译](../server/app/services/elmer_electrodes.py)

本机文件系统的两个实际算例分别施加 5 V 和 20 A。2 m² 电极的电流密度为 10 A/m²，电势仍为 0–5 V，沿棒轴的电流密度指向负 X。实际 VTU 和独立解析解的误差低于 `4 × 10⁻¹⁵`。原 SIF 保持原字节，缺少网格标签或 SIF 重复赋值会在派发前拒绝。 [原生参照](../../tmp/FF-CB08-elmer-electric-20261123/native.json) · [文件系统](../../tmp/ff-cb08-elmer-electric-2dmqs4za/electric.json)

PostgreSQL 与认证 SeaweedFS 完成两组相同物理算例，封存 SIF 摘要和数值观测与文件系统一致。Edge 从真实作业历史选中电流工况，验证电极面积 2 m²、总电流 20 A、原生密度 10 A/m²，以及键盘选择完整 SIF 摘要。三种视口宽度的成果面板均可读，下载原生 VTU 的摘要与该发布归档相符。 [认证对象存储](../../tmp/ff-cb08-elmer-electric-nyx7yb3j/electric.json) · [临时 PostgreSQL 清理](../../tmp/terminal-agent-postgres-m5ojqw93/cleanup.json) · [Edge](../../tmp/cb08-h18-edge-electrodes-_3a3h38_/browser.json)

## Elmer 电极接触电阻

直流电极使用 `physicalType impedance`、`unit Ohm`、正的恒定电阻，以及带 `value` 和 `unit V` 的 `externalPotential`。使用当前安装版的 `StatCurrentSolveVec / StatCurrentSolver`，平台从原生网格面积 A 和电阻 R 计算单位面积电导 `1/(A·R)`。私有 SIF 使用 `External Conductivity` 和 `External Potential` 构成 Robin 边界。Elmer 26.2 的关键词表会提示 `External Conductivity`，因此算例使用 `CHECK KEYWORDS Warn`。同版的旧电流模块实测忽略该项，平台在派发前拒绝不匹配的模块。 [Elmer 发行版源代码](https://github.com/ElmerCSC/elmerfem/blob/release-26.2.1/fem/src/modules/StatCurrentSolveVec.F90#L550-L638) · [旧模块原生日志](../../tmp/FF-CB08-elmer-impedance-20261123/legacy-solver.log)

同一根电导率 2 S/m、截面 2 m² 的导体接 6 V 外部电势。0.25 Ω 和 0.75 Ω 的接触电阻分别生成 2 和 0.6666667 S/m²，实际电流为 12 和 6 A，末端电势为 3 和 1.5 V。文件系统的两个真实发布与独立解析及原生数值一致。0 Ω 和缺少外部电势的声明均在保存前拒绝，算例修订保持。 [独立原生](../../tmp/FF-CB08-elmer-impedance-20261123/native.json) · [文件系统](../../tmp/ff-cb08-elmer-impedance-bt98x2yy/impedance.json)

PostgreSQL 和认证 SeaweedFS 的两组数值与封存 SIF 摘要均与文件系统一致。Edge 实测以键盘查看 0.75 Ω、6 V、0.6666667 S/m²，三种宽度均可读，归档 VTU 下载的摘要与所选发布一致。 [认证对象存储](../../tmp/ff-cb08-elmer-impedance-yjvs6a2_/impedance.json) · [临时 PostgreSQL 清理](../../tmp/terminal-agent-postgres-aynr2d0u/cleanup.json) · [Edge](../../tmp/cb08-h18-edge-impedance-041kjhfz/browser.json)

## 声学出口阻抗

`acousticFoam` 的出口网格 `patch` 支持已确认的 `physicalType impedance`，角色为 `outlet` 或 `farField`，声明正的常量和单位 `Pa*s/m`。平台读取原生 `transportProperties.rho`，用 `Z/ρ` 得到 `acousticWaveTransmissive.advectiveSpeed`。声压字段 `pa` 使用 Pa，可从 kPa 换算，也可通过现有时间表、脉冲和初值入口生成。阻抗适用于实值恒定的平面波边界。 [原生边界条件](https://doc.openfoam.com/2312/tools/processing/boundary-conditions/rtm/derived/outlet/acousticWaveTransmissive/) · [生成器](../server/app/services/openfoam_boundary_inputs.py)

真实 OpenFOAM 2406 在 128 单元直管中发射 1 Pa 脉冲，介质密度为 1.2 kg/m³、声速为 100 m/s。匹配阻抗 120 Pa·s/m 的测量反射为 `−0.0002927 Pa`，阻抗 360 Pa·s/m 时为 `0.4977406 Pa`，解析式 `(Z−ρc)/(Z+ρc)` 给出 0 和 0.5。文件系统两组托管求解均确认 D，最终 128 个声压值与实际独立原生执行逐项一致。负阻抗在保存时拒绝，算例修订保持。 [独立原生](../../tmp/FF-CB08-acoustic-impedance-20261123/native.json) · [文件系统](../../tmp/ff-cb08-acoustic-impedance-avrjwbjg/impedance.json)

认证 SeaweedFS 与 PostgreSQL 的两组真实工况均确认 D。各自的原生声压摘要、逐单元值及反射振幅与文件系统一致，负阻抗保存拒绝也通过。 [认证对象存储](../../tmp/ff-cb08-acoustic-impedance-1p8e27g9/impedance.json) · [临时 PostgreSQL 清理](../../tmp/terminal-agent-postgres-xbcrd_ft/cleanup.json)

## 初值来源界面

求解结果的产物追溯显示历史来源版本和时刻、网格映射覆盖、解析公式或 CSV 来源。Edge 通过冷启动 API 读取保留的原生算例，在 1440、900 和 640 像素宽度检查内容和横向溢出。异网格算例用键盘选择结果，显示 64→512 单元和全部覆盖，再按 Enter 打开确切的历史发布。 [历史版本](../../tmp/cb08-h18-edge-mapping-44oh6az2/browser.json) · [解析公式](../../tmp/cb08-h18-edge-analytical-p8z_ayk8/browser.json) · [CSV 采样](../../tmp/cb08-h18-edge-measurement-k0781s2m/browser.json)

## T02 验收

2026-11-23，FF-CB08 按原计划验收。六种时间曲线和五种初值均有真实求解对照，OpenFOAM、CalculiX 和 Elmer 的边界与载荷映射覆盖各自使用的物理量。未确认声明、错误引用或单位、原生冲突及映射域外均有独立拒绝证据。另通过浏览器的初值来源和封存成果历史读取。本阶段为实际工程求解与配置验证，模型调用 0 次。 [T02 汇总](../../tmp/FF-CB08-elmer-impedance-20261123/validation.json) · [台账](../../tmp/stage-ledger-evidence-20261123-ff-cb08.json)
