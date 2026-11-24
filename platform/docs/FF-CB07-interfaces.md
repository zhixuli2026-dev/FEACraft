# FF-CB07 命名接口与原生耦合证据

`interfaces` 校验命名条目、接口类型、耦合类型及显式双侧区域和网格边界。元数据的配置检查提示缺失区域。周期接口允许用一个区域名称表达同区域的两侧。校验在原字典解析结果上解释列表，已封存工程输入的解析和摘要保持可追溯。 [schema](../server/app/services/casedict_schema.py) · [配置检查](../server/app/services/case_meta.py)

托管单网格的 `periodic` 和 `cyclic` 声明核对原生边界互为 `neighbourPatch`，并要求面数及实际类型适配。SQLite 文件系统及 PostgreSQL 认证 SeaweedFS 均用真实 `blockMesh` 和 `icoFoam` 验证周期边界贯通，6 行速度沿周期方向一致，`0.1 s` 时最大速度 `0.845220174491 m/s`，算例确认 D。将一侧引用改为普通墙时，托管作业确认停止，算例仍在 B，无新网格发布。 [原生周期基准](../../tmp/FF-CB06-regions-20261122/native-cyclic-0wozy3ee/proof.json) · [文件系统回执](../../tmp/ff-cb07-periodic-interfaces-flab3ugj/periodic.json) · [认证 SeaweedFS 回执](../../tmp/ff-cb07-periodic-interfaces-nxxjw4jm/periodic.json) · [错误配对回执](../../tmp/ff-cb07-periodic-interfaces-tkdjdf5m/periodic.json) · [OpenFOAM 周期边界](https://doc.openfoam.com/2212/tools/processing/boundary-conditions/rtm/derived/coupled/cyclic/)

五区域共轭传热验证了 `coupledWall` 的实际 `sampleRegion` 与 `samplePatch` 双向配对。SQLite 文件系统及 PostgreSQL 认证 SeaweedFS 都发布了完整结果并确认 D，公开接口拒绝非法耦合值和错误的双侧声明。 [文件系统 CHT](../../tmp/ff-cb06-cht-managed-siuyr5th/cht.json) · [认证 SeaweedFS CHT](../../tmp/ff-cb06-cht-managed-rfy2g36x/cht.json) · [接口核验实现](../server/app/services/openfoam_cht.py)

## CalculiX 机械接触

在 `interfaces` 声明 `type contact`、`coupling mechanical`，用 `patches` 按从面、主面的顺序引用原生 `*SURFACE`。在 `contactLaw` 选择 `model linearPenalty`，填写正的 `normalStiffness` 和 `unit "Pa/m"`。原生模型采用 m 和 N。托管作业将面到面线性罚接触写入私有副本，启动和发布均核验该副本摘要。原始输入保留。 [编译实现](../server/app/services/calculix_contact.py) · [CalculiX 接触对](https://web.mit.edu/calculix_v2.7/CalculiX/ccx_2.7/doc/ccx/node177.html) · [压力与过闭合关系](https://web.mit.edu/calculix_v2.7/CalculiX/ccx_2.7/doc/ccx/node248.html)

SQLite 文件系统使用真实 CCX 压缩两个单位立方体。接触压力 9.850022 Pa，固定端合反力 9.850024 N，过闭合约 0.985 µm，切向力为零。实际 `.dat` 数值逐项与独立原生模型一致。错误刚度单位在保存时拦截，未定义单元面在启动前被拒绝。 [本地接触数值](../../tmp/ff-cb07-calculix-contact-azqynsjq/calculix-contact.json) · [缺失单元面](../../tmp/ff-cb07-calculix-contact-c983s5u0/calculix-contact.json)

PostgreSQL 和认证 SeaweedFS 复验了同一个机械接触，压力、合反力和过闭合与文件系统逐项一致。原始输入、发布结果与停止证明均可读取，隔离 PostgreSQL 库已清除。 [认证对象存储](../../tmp/ff-cb07-calculix-contact-9wichxxb/calculix-contact.json) · [公开追溯回执](../../tmp/ff-cb07-calculix-contact-6r51lf3m/calculix-contact.json)

求解成果显示封存版本的机械接触接口、从面到主面的顺序与法向刚度。Edge 经冷启动 API 读取实际求解发布，在 1440、900 和 640 像素宽度检查显示与横向溢出，并用键盘预览原生 DAT，核对计算得到的 9.850022 Pa。 [Edge 回执](../../tmp/cb08-h18-edge-contact-o0tc_nnd/browser.json) · [640 像素界面](../../tmp/cb08-h18-edge-contact-o0tc_nnd/contact-640.png)

## 静态 Overset 热耦合

在 `interfaces` 声明 `type overset` 和 `coupling thermal`，并在 `overset` 填写原生 `patch`、从 0 开始的连续 `zones` 和 `interpolation inverseDistance`。该路径使用 `overLaplacianDyMFoam` 的 `staticOversetFvMesh`。网格发布记录对应的原生边界及面数。求解准备验证 `system/fvSchemes`、`constant/dynamicMeshDict` 和无量纲 ASCII `0/zoneID`，求解发布绑定原始区域场摘要与 native 日志中的单元分类。 [验证实现](../server/app/services/openfoam_overset.py) · [OpenFOAM Overset 文档](https://doc.openfoam.com/2306/tools/processing/numerics/overset/)

独立 OpenFOAM 2406 导热基准和两套托管存储均求解 149 个单元，其中 132 个求解、17 个插值、0 个孔洞。两个区域分别有 100 和 49 个单元。两套托管结果均确认 D，与独立基准的 149 个最终温度逐一相等，范围为 274.2771032–299.0644337 K。普通墙替代插值边界时流程停在 B，`zoneID` 含未声明区域时流程停在 C，未派发求解。首轮托管执行完成了原生网格检查，但新生成的 `constant/cellToRegion` 被输入保护拦截。修正后该原生诊断随网格发布归档到 `mesh-diagnostics/cellToRegion`，原有输入仍逐项比对。 [原生基准](../../tmp/FF-CB07-overset-20261123/native-direct-bab453df/overset-native.json) · [文件系统](../../tmp/ff-cb07-overset-5mqvzqnq/overset.json) · [认证对象存储](../../tmp/ff-cb07-overset-78jgha5n/overset.json) · [错误边界](../../tmp/ff-cb07-overset-9p7wq30h/overset.json) · [错误编号](../../tmp/ff-cb07-overset-3tem_vsy/overset.json) · [首轮记录](../../tmp/ff-cb07-overset-_saiyl2i/overset.json)

Edge 用键盘打开对应的求解发布，在 1440、900 和 640 像素宽度读取插值方法、区域与实际单元分类。此轮为实际求解、对象存储和浏览器验收，模型调用为 0。 [Edge 回执](../../tmp/cb08-h18-edge-overset-ite33hvu/browser.json)

## 移动 Overset 热耦合

在已有 `overset` 热接口上，通过 `regions` 绑定原生 `cellZone`，启用 `moving` 并声明 `motion.type linearMotion` 与 m/s 速度。`physicalProblem.movingFrame.region` 可显式指向该区域。原生 `constant/dynamicMeshDict` 使用 `dynamicOversetFvMesh`、`motionSolver solidBody` 和 `solidBodyMotionFunction linearMotion`，速度与区域须和声明相符。原生 `zoneID` 负责插值区域编号，`cellZone` 负责选择移动的网格。 [OpenFOAM 2406 动态 Overset](https://api.openfoam.com/2406/dynamicOversetFvMesh_8H_source.html) · [输入与位移核验](../server/app/services/openfoam_overset.py)

两套存储用 149 单元实算至 3 s，移动区 128 个点以 0.02 m/s 平移 0.06 m，背景 242 个点保持原坐标。平台从原生点场核验位移误差约 `1.67 × 10⁻¹⁶ m`。三个写出时刻的 points 与独立运行逐字节相同，149 个末态温度逐值相同，两个流程都确认 D。冲突速度在 C 阶段拦截，未派发求解。静态插值实算回归通过。Edge 从冷启动的 API 读取封存版本，在三种视口核对插值和运动数值。 [文件系统](../../tmp/ff-cb07-overset-a1p_atsy/overset.json) · [认证对象存储](../../tmp/ff-cb07-overset-mx27g8a3/overset.json) · [冲突拒绝](../../tmp/ff-cb07-overset-kdfnu9dl/overset.json) · [静态回归](../../tmp/ff-cb07-overset-i3flar6r/overset.json) · [Edge](../../tmp/cb08-h18-edge-overset-e98s8u56/browser.json) · [构建与磁盘](../../tmp/FF-CB07-overset-20261123/build-disk-moving.json)

## OpenFOAM 压力到 CalculiX 的单向载荷

同一算例完成原生 OpenFOAM 求解与 `pressureForce` 曲面积分后，从作业面板的「新建任务 → 开源求解 → CalculiX」选择结构输入和「OpenFOAM 压力合力」。读取当前后处理发布，或指定历史发布 ID，再选择曲面、写出时刻和 CalculiX `*NSET`。API 为 `POST /api/v1/cases/{id}/calculix:solve` 的 `pressure_transfer`，字段为 `publication_id`、`surface`、`patch`、`time`、`node_set`。 [前端入口](../web/src/panels/ExecutionLauncher.tsx) · [求解接口](../server/app/routers/calculix.py)

默认将发布的牛顿合力按原坐标方向均分到静力节点集。流体与结构需使用同一坐标方向，结构输入使用 m 和 N。作业封存获选 `metrics.json`、发布清单与确切的 CalculiX 私有输入。成果页可读来源、目标与三分量合力。空间分布按下文的对应节点路径处理。 [压力来源核验](../server/app/services/calculix_pressure_transfer.py) · [原生节点载荷](../server/app/services/calculix_boundary_inputs.py)

实际 OpenFOAM 32 单元算例在 `0.02 s` 的 1 m² 墙产生 `(10, 0, 0) N`。实际 CalculiX 的位移 10 mm、轴应力 10 Pa、支座反力 −10 N，均符合弹性解析值。两套存储的压力场、私有 CalculiX 输入和 DAT 与独立原生复算一致。错误时刻、边界、节点集、未知发布和重复原生力均在派发前拒绝。 [文件系统](../../tmp/ff-c10-udf-pressure-transfer-_83a3_b_/pressure-transfer.json) · [认证对象存储](../../tmp/ff-c10-udf-pressure-transfer-38k6cm3_/pressure-transfer.json)

真实 Edge 在三个视口通过选择、键盘、无效发布反馈、实际提交、原任务查询和求解成果回读。专项只涉及求解器、浏览器和存储，真实模型 0 次，模拟 0 次。 [浏览器回执](../../tmp/ff-cb07-edge-pressure-transfer-kwj8nnsq/edge.json) · [640 像素提交界面](../../tmp/ff-cb07-edge-pressure-transfer-kwj8nnsq/launch-640.png) · [构建磁盘记录](../../tmp/FF-CB07-pressure-transfer-20261123/build-disk.json)

## 声压快照到 CalculiX 的单向载荷

`acousticFoam` 原生压力波方程输出 `pa`。在 `postProcessing` 声明该场的单位为 `Pa`，选用 `pressureForce` 提取边界合力。作业面板允许选择带「声压 pa」标记的快照。CalculiX 发布保存源字段、曲面、写出时刻、合力和上游发布 ID。结构求解针对选定时刻施加等效静力载荷，压力符号及源网格的坐标方向沿传递链保留。结构振动和声场反馈仍在耦合范围中跟踪。 [OpenFOAM 2406 声学控制方程](https://api.openfoam.com/2406/acousticFoam_8C_source.html) · [归档压力检查](../server/app/services/calculix_pressure_transfer.py)

64 单元声管在右侧 `1/4096 m²` 面按不同时刻施加 −40.96 和 +40.96 Pa。两套存储得到约 −0.01 和 +0.01 N，CalculiX 端面分别移动 −10 和 +10 µm，支座反力反向。封存的声压场与独立 OpenFOAM 逐字节相同。VTK 读回的浮点精度使合力绝对差约 `2.24 × 10⁻¹⁰ N`，独立 CalculiX 的位移、轴应力和反力逐值一致，原始 DAT 字节存在数值舍入差。Edge 选中声压快照、提交实际作业并读取已发布载荷。原流体压力路径也再跑通过。 [SQLite 与 Edge](../../tmp/ff-cb07-edge-acoustic-transfer-2zn3spu4/acoustic-transfer.json) · [PostgreSQL 和认证对象存储](../../tmp/ff-c10-udf-acoustic-transfer-kvlzk1q_/acoustic-transfer.json) · [窄屏结果](../../tmp/ff-cb07-edge-acoustic-transfer-2zn3spu4/result-640.png) · [流体压力回归](../../tmp/ff-c10-udf-pressure-transfer-4gpgdc06/pressure-transfer.json) · [构建与磁盘](../../tmp/FF-CB07-acoustic-transfer-20261123/build-disk.json)

## Elmer 导体连接与焦耳热

两侧在 `regions` 使用 `role electromagneticRegion`，`meshRegion` 引用原生网格的数值体标签。在 `interfaces` 声明 `type coupledWall`、`coupling electromagnetic`、两侧 `regions` 与 `elmerElectrical.boundaryTag`。该标签必须为两侧共节点的原生内部面，两侧各绑定一个 SIF `Body`，界面上不能另设边界条件。稳态方程先运行 `StatCurrentSolver`，再运行 `HeatSolver`。平台核对各原生体的正标量电导率，并在封存 SIF 中给两个体传递焦耳热。 [Elmer 26.2 静态电流](https://raw.githubusercontent.com/ElmerCSC/elmerfem/release-26.2.1/fem/src/modules/StatCurrentSolve.F90) · [网格接口核对](../server/app/services/elmer_electrical_interface.py) · [封存输入生成](../server/app/services/elmer_joule.py)

两段长度均为 0.5 m，电导率分别为 2 和 8 S/m，两端为 0 和 5 V。实际中间面电势 4 V，法向电流密度 −16 A/m² 在两段连续。最高温度 310 K，焦耳热在中间节点投影为 80 W/m³。双存储的 12 个节点与独立原生数值逐值相同。首轮脚本把 VTU 中的 3 个边界单元也计为体单元，修正后按实际类型核对 2 个体单元及 3 个面。重复两侧、无原生面、选用已有边界条件的外侧面、两侧绑定同一原生体均拒绝。 [本地实算](../../tmp/ff-cb07-elmer-interface-b8ba98u9/electrical-interface.json) · [认证对象存储](../../tmp/ff-cb07-elmer-interface-56khgdfv/electrical-interface.json) · [单体焦耳热回归](../../tmp/ff-c01-elmer-joule-bf2vki9u/joule.json)

Edge 用键盘打开封存发布，在 1440、900 和 640 像素读取两个体的电导率、接合区域、原生面标签和封存 SIF 摘要。通过键盘下载的 VTU 与发布清单摘要相同。前端生产构建通过。构建前后 C 盘可用空间为 17,714,241,536 B 和 17,702,412,288 B，Docker 构建缓存回收 0 B。 [Edge 回执](../../tmp/cb08-h18-edge-elmer-interface-fr4fvi37/browser.json) · [构建与磁盘](../../tmp/FF-CB07-elmer-interface-20261123/build-disk.json)

## 对应节点的非均匀压力载荷

在原 `pressureForce` 边界积分添加 `nodalLoads true`。后处理按原生面的有向面积将压力积分为各顶点力，归档坐标和牛顿力。新建 CalculiX 任务时选择「按对应节点传递」，API 使用 `distribution conformalNodal`。结构的全局米制节点与流体壁面点逐一对应，默认容差 `10⁻⁷ m`，API 可使用 `match_tolerance_m` 指定。这次作业逐节点生成 `*CLOAD`，封存上游的计算发布。 [OpenFOAM 压力面积积分](https://doc.openfoam.com/2606/tools/post-processing/function-objects/forces/forces/) · [匹配与封存](../server/app/services/calculix_pressure_transfer.py)

真实壁面上下两半载荷为 20 和 40 Pa，9 个点的总力 `(30, 0, 0) N`，关于全局原点的力矩 `(0, 15, −17.5) N·m`。CalculiX 结构两端对应位移 20 和 40 mm，支座合反力 −30 N。两套存储压力与结构 DAT 逐字节相同，三项结构指标与独立计算一致。节点位置冲突被拒绝，均布压力的旧任务复验成功。Edge 在三个视口真实提交，核对原请求和已发布节点数。首次试件的 4 单元网格被 `checkMesh` 完整检查拒绝，增加到 8 单元后通过。 [文件系统](../../tmp/ff-c10-udf-conformal-pressure-ubbdxw4b/conformal-pressure.json) · [认证对象存储](../../tmp/ff-c10-udf-conformal-pressure-7n6jrcyf/conformal-pressure.json) · [Edge](../../tmp/ff-cb07-edge-conformal-pressure-g4swopl2/conformal-pressure.json) · [首次网格](../../tmp/ff-c10-udf-conformal-pressure-9pl_83jl/conformal-pressure.json) · [均布旧任务](../../tmp/ff-c10-udf-pressure-transfer-eyw7__mf/pressure-transfer.json)

## CalculiX 位移反馈到 OpenFOAM

用同一算例的 CalculiX 发布作为 `interfaces` 中 `coupledWall`／`conformal` 的 `fluidStructure` 接口来源。在 `structuralDisplacement` 填写 `publicationId`、`inputPath`、FRD 的 `sourceTime`、`movingPatch`、`fixedPatch` 和 `freePatches`。移动边界在原生 `0/U` 使用 `movingWallVelocity`。平台读取所选时刻的 ASCII FRD `DISP` 场，在全局米制坐标逐点匹配，经原生壁面点顺序生成封存 `pointDisplacement`，由串行 `pimpleFoam` 的 `displacementLaplacian` 从初始时刻执行。当前工作流每次选定一个结构快照，随后可用新流场发起下一轮。 [原生点边界](https://api.openfoam.com/2406/classFoam_1_1pointPatch.html) · [映射与验证](../server/app/services/openfoam_structural_feedback.py)

本机工作流从压力分布到 CalculiX，再以同一 FRD 发布驱动第二次 OpenFOAM 求解。`hot` 的 9 个点发生非均匀位移，另 9 个固定点保持位置。最大位移 0.0412310563 m，实际坐标误差约 `2.28 × 10⁻¹⁵ m`。0.01 与 0.02 s 的原生点文件及 0.02 s 的 U、p 与独立运行逐字节一致。Edge 在三个视口按版本确认来源和结果。托管工作流会封存来源 FRD，标准包目前需要该来源的移植支持。 [文件系统](../../tmp/ff-c10-udf-structural-feedback-i1dtr8tm/structural-feedback.json) · [独立原生](../../tmp/FF-CB07-conformal-pressure-20261123/native-feedback/solve.log) · [Edge](../../tmp/cb08-h18-edge-structural-feedback-pv3shcf1/browser.json)

认证对象存储的同一工作流也确认 D。两套存储均核对封存 FRD 与计算出处，两个点场和末态 U、p 的四个摘要一致。早期原生 worker 导入了额外服务依赖，拆分后从 OpenFOAM 压力开始重跑通过。原生 FRD 缺少所选时刻的情况已验证拒绝。FF-CB07 按 T02 汇总命名接口与热、机械、周期、Overset、流固、电磁和声学的支持矩阵。本专项真实模型调用 0 次，模拟 0 次。 [认证对象存储](../../tmp/ff-c10-udf-structural-feedback-tahlr2mp/structural-feedback.json) · [T02 验收](../../tmp/FF-CB07-conformal-pressure-20261123/t02-validation.json)
