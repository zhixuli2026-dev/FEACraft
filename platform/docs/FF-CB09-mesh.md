# FF-CB09 网格单元族、方向加密和边界层

`caseDict` 的 `mesh.elementFamilies` 支持单个名称或不重复的名称列表。托管阶段从完整 `checkMesh` 提取实际单元数、各单元族和几何维度，然后写入同一发布的质量报告。真实二维挤出网格的六面体柱可对应 `quad`，二维三棱柱可对应 `tri`。三维声明按原生单元族核验。实际 Gmsh 网格的 64 个六面体在两套存储通过，改为 `tet` 声明时拦住 C。 [文件系统](../../tmp/ff-b18-gmsh-artifacts-y_1zlttu/mesh-artifacts.json) · [认证对象存储](../../tmp/ff-b18-gmsh-artifacts-3m627fnl/mesh-artifacts.json) · [声明不匹配](../../tmp/ff-b18-gmsh-artifacts-17qm08nv/mesh-artifacts.json)

显式 `mesh.dimension` 与 `checkMesh` 的实际几何维度一致才能发布。三维 Gmsh、三维现有网格导入分别通过真实任务，导入包离线复验也通过。将三维网格声明为二维时，原生检查完成后流程停在 B。`mesh.meshSource` 与当前网格器冲突时在派发前拦截。 [生成网格](../../tmp/ff-b18-gmsh-artifacts-6jciii7_/mesh-artifacts.json) · [导入网格](../../tmp/ff-cb09-imported-gmsh-l1huj1e1/mesh-artifacts.json) · [维度负例](../../tmp/ff-b18-gmsh-artifacts-902k2_pt/mesh-artifacts.json) · [来源负例](../../tmp/ff-b18-gmsh-artifacts-9u2vmbo7/mesh-artifacts.json)

`mesh.adaptivity h` 可配置 `mesh.refinement { min (0 0 0); max (0.5 1 1); axis x; }`。当前托管路径对通过完整检查的 `blockMesh` 生成网格执行。原生 `topoSet` 选中 32 个单元，`refineMesh` 按 x 方向将 64 个单元增至 96 个。程序在独立工作目录运行工具并保留原初值。前后网格检查及加密日志的原件、大小和 SHA-256 随发布保存。两个实际求解的解析温度误差均为 0，确认 D。空选区不会发布网格。首次托管试验缺少原生 `writeInterval`，补齐后通过。早期三轴局部加密产生凹单元，被严格检查拦住。 [文件系统](../../tmp/ff-cb09-directional-refinement-b4y_vwrh/directional-refinement.json) · [认证对象存储](../../tmp/ff-cb09-directional-refinement-vluwap4k/directional-refinement.json) · [空选区](../../tmp/ff-cb09-directional-refinement-862hkllj/directional-refinement.json) · [OpenFOAM topoSet](https://doc.openfoam.com/2606/tools/pre-processing/mesh/manipulation/topoSet/)

平面结构 `blockMesh` 可设置 `mesh.boundaryLayerRequired true`，并在 `mesh.boundaryLayers` 提供 `patch`、法向 `axis`、`minLayers`、`maxFirstLayerHeight`（米）和 `maxGrowth`。检查从实际壁面的 owner 经原生内部面及 neighbour 追踪每层单元，测量沿法向的真实厚度和增长率。实际 `xMin` 的 16 个壁面单元各测到三层，厚度约为 0.066667、0.133333 和 0.266667 m，最大增长率约为 2。指向另一端较厚的 `xMax` 时，首层高度超过上限，流程停在 B。两套存储成功到 D，温度解析误差约 `3.5e-10 K`。 [文件系统](../../tmp/ff-cb09-boundary-layers-zcbghl8u/boundary-layers.json) · [认证对象存储](../../tmp/ff-cb09-boundary-layers-0sd6fc56/boundary-layers.json) · [实际壁面错误](../../tmp/ff-cb09-boundary-layers-l8z6tubl/boundary-layers.json) · [OpenFOAM 网格文件](https://doc.cfd.direct/openfoam/user-guide-v13/mesh-files) · [blockMesh 网格密度变化](https://doc.openfoam.com/2606/tools/pre-processing/mesh/generation/blockMesh/)

在 caseDict 面板打开「产物追溯」，选择网格发布即可读取所选版本的证据，查看质量门禁、实际单元族、加密数量和边界层厚度。Edge 使用键盘选中两个真实发布，并核对请求指向对应版本。 [加密发布](../../tmp/ff-cb09-directional-refinement-4aersdzr/browser.json) · [边界层发布](../../tmp/ff-cb09-boundary-layers-0ea94jy4/browser.json)

两套存储下载的标准算例包均用封装时生成的原生字典重建了同一 96 单元网格。离线 `Allrun.pre` 检查网格，`Allrun` 的结果场与对应托管求解逐字节一致，`Allclean` 清理生成文件后保留原输入。无方向加密的旧格式算例再通过三次求解。 [文件系统离线](../../tmp/ff-cb09-directional-refinement-dhmxi3tc/directional-refinement.json) · [认证对象存储离线](../../tmp/ff-cb09-directional-refinement-bv1mp593/directional-refinement.json) · [未加密算例兼容](../../tmp/ff-c17-standard-openfoam-ap6w4tmu/standard-case.json)

选择 `mesh.source importedGmsh` 后，将 ASCII MSH 2 原件上传到当前运行目录的 `system/mesh.msh`。托管流程只执行 `gmshToFoam` 和 `checkMesh`，报告绑定上传原件、保留副本、转换计数及实际网格。两套存储均完成 64 单元求解和离线包复算。历史整件与分段下载可读取原发布副本，其他算例返回 404。MSH 4 格式在启动网格作业前拒绝。 [文件系统导入](../../tmp/ff-cb09-imported-gmsh-hjhnjf2e/mesh-artifacts.json) · [认证对象存储导入](../../tmp/ff-cb09-imported-gmsh-7ggla7qh/mesh-artifacts.json) · [格式负例](../../tmp/ff-cb09-imported-gmsh-64v8wlhi/mesh-artifacts.json)

2026-11-23，`mesh.source importedPolyMesh` 读取单区域 OpenFOAM 网格。将 `points`、`faces`、`owner`、`neighbour` 和 `boundary` 放在运行目录的 `system/polyMesh`，按来源声明 `meshSource imported`、`solverNative` 或 `external`。平台从封存输入复制到私有 `constant/polyMesh`，运行实际完整 `checkMesh`，核对全部复制文件和输出摘要。标准包采用相同相对输入位置。缺少 `neighbour` 时在 B 阶段阻止派发。

SQLite 文件系统的 `solverNative` 和 PostgreSQL 认证 SeaweedFS 的 `external` 路径保留原始五文件字节，验证的 64 个单元均用真实 `laplacianFoam` 求解到 D。温度解析误差为 0 K，离线标准包的结果逐字节一致。 [文件系统原生网格](../../tmp/ff-cb10-numerical-pjrs_rfs/numerical.json) · [对象存储外部网格](../../tmp/ff-cb10-numerical-18yidus4/numerical.json) · [缺少原件组件](../../tmp/ff-cb10-numerical-_hw4izqh/numerical.json)

同一导入允许每个必要文件采用普通版或 `.gz` 版。原始 gzip 从公开文件接口上传，发布记录原件摘要，托管运行副本解压并核对实际拓扑。两套存储的 gzip 输入均完成 C、D 和离线复跑，温度解析误差为 0 K，原件字节保持。同一组件同时提供普通版和 gzip 版时在 B 拒绝派发。首轮上传试验继承宿主代理设置后未成功，隔离测试客户端直接连接对应本机 API 后通过。 [gzip 文件系统](../../tmp/ff-cb10-numerical-qbrxln28/numerical.json) · [gzip 认证对象存储](../../tmp/ff-cb10-numerical-bip3h3hj/numerical.json) · [双版冲突](../../tmp/ff-cb10-numerical-ps6fqhl7/numerical.json)

本机原生单单元和双单元试验覆盖紧凑点表、重复 owner 和空 neighbour。修正后测得一层 1 m 和两层各 0.5 m。当前网格运行属于原生工具与存储验收，模型调用 0。 [阶段证据](../../tmp/FF-CB09-mesh-20261122/validation.json)

| 当前起点 | 已实测能力 |
|---|---|
| `blockMesh` | 结构六面体、局部单轴加密、指定平面壁面层厚度 |
| `gmsh` | 从 `.geo` 划分实际网格并转换为 OpenFOAM |
| `importedGmsh` | 转换现有 ASCII MSH 2，保存和下载原件 |
| `importedPolyMesh` | 单区域 OpenFOAM 原件、完整检查、实际导热和离线复算 |
| `importedOpenFOAM` | 已有多区域网格的完整检查和共轭传热 |

网格来源、质量预设与网格无关性记录在 FF-B15。`snappyHexMesh` 和 `cfMesh` 保留在 FF-B17。 [全功能计划](full-function-delivery-plan.md) · [多区域共轭传热](FF-CB06-regions.md)

2026-11-23，四种原生网格质量预设通过工作流和版本化产物验收。双存储验证生产预设和阈值覆写，严格长宽比负例拦截在 B。 [网格质量预设](FF-B15-mesh-presets.md)

## 网格质量报告

实际 Pandoc 生成的 HTML 与 DOCX 从所用网格发布读取质量记录。报告展示预设、维度、原生单元族、加密前后数量和选区数量，壁面层展示实测厚度范围及增长率。文件系统的方向加密报告显示 64→96 单元及 32 个选中单元。文件系统与认证 SeaweedFS 的壁面层报告显示 `xMin`、16 个壁面单元、约 0.066666667、0.13333333、0.26666667 m 三层厚度。验收比对了正文、可下载文件摘要和报告所引用的网格发布。 [方向加密报告](../../tmp/ff-cb09-mesh-report-x_qz10k4/mesh-report.json) · [文件系统壁面层](../../tmp/ff-cb09-mesh-report-2go112hu/mesh-report.json) · [认证对象存储壁面层](../../tmp/ff-cb09-boundary-layers-i0w4wym8/boundary-layers.json)

## MOOSE 的 h、p 和 hp 细化

在 `mesh.adaptivity` 选择 `h`、`p` 或 `hp`，并在 `mesh.mooseAdaptivity` 填写 `cycles`、`maxHLevel` 及以米计的 `min`、`max` 三分量。该路径核对 MOOSE 稳态 `.i` 的原生 `Adaptivity` 和 `BoxMarker`。p 和 hp 采用 `HIERARCHIC` 单元插值族。原生 `NumElements` 和 `NumDOFs` 分别提供活动单元数和非线性自由度，所有实际写出的 Exodus 阶段与 CSV 随版本封存。 [MOOSE 网格自适应](https://mooseframework.inl.gov/syntax/Adaptivity/) · [配置与回执](../server/app/services/moose_mesh_adaptivity.py)

双存储均完成两轮。h 从 4→8→16 个单元、5→9→17 个自由度。p 始终为 4 个单元，自由度为 5→9→13，采样点结果 `0.118231` 对应解析温度场。hp 在该算例实际选择了 h 细化，历史与 h 相同。CSV 与独立 WSL 逐字节一致。首轮 p 在已接近舍入误差的第三次 Newton 运行出现线搜索失败，按数值设置加入绝对残差容差 `10⁻¹²` 后正常收敛。配置冲突被拒绝。原生空选区未发布新版本，已有结果保持。 [文件系统](../../tmp/ff-cb09-moose-adaptivity-lbr_mqi7/mesh-adaptivity.json) · [认证对象存储](../../tmp/ff-cb09-moose-adaptivity-30h5a5kp/mesh-adaptivity.json) · [首轮 p 日志](../../tmp/FF-CB09-moose-adaptivity-20261123/native/p/native.log) · [固定与自适应时间步回归](../../tmp/ff-c02-moose-numerical-sifr1l1z/numerical.json)

在「作业与日志 → 求解成果」查看细化方式、活动单元数和自由度。Edge 在三种视口通过键盘查阅 p 细化结果并下载最后一个 Exodus，下载摘要与历史发布一致。 [Edge 回执](../../tmp/cb08-h18-edge-moose-adaptivity-wb7vquzf/browser.json) · [前端构建和磁盘](../../tmp/FF-CB09-moose-adaptivity-20261123/build-disk.json)

## MOOSE 目标驱动细化

`mesh.adaptivity goalOriented` 在同一 `mooseAdaptivity` 中增加 `nativeRefinement` 和 `goal`。目标指定 `postprocessor`、`value` 与 `absoluteTolerance`。稳态原生输入使用 `PointValue` 采样、`ParsedFunction` 和 `FunctionAux` 计算选区内的目标误差，并由 `ValueThresholdMarker` 决定是否继续细化。归档记录每轮活动单元数、自由度和采样值，以及第一次达到目标的轮次。当前路径使用用户指定的标量目标和选区。 [MOOSE 原生阈值标记](https://mooseframework.inl.gov/source/markers/ValueThresholdMarker.html) · [生成与核验](../server/app/services/moose_mesh_adaptivity.py)

实际 p 细化在第 1 轮把采样值从 0.10275 改为 0.118231，达到 `10⁻⁸` 的目标容差。四个活动单元的自由度从 5 增至 9，随后三轮保持 9。双存储 CSV 和最终 Exodus 的摘要一致。已经满足目标的初始网格可正常发布。目标声明与原生值冲突会拒绝派发，原生计算结束仍未达标会保留上一次成功发布。Edge 在 1440、900 与 640 像素宽度显示上述实测值，并核对归档网格的下载摘要。 [文件系统](../../tmp/ff-cb09-moose-goal-dndigdeb/mesh-goal.json) · [认证对象存储](../../tmp/ff-cb09-moose-goal-0kwju8a0/mesh-goal.json) · [原生基准](../../tmp/FF-CB09-moose-adaptivity-20261123/native/target-final/native.log) · [Edge](../../tmp/cb08-h18-edge-moose-goal-f1sd93p_/browser.json)

## OpenFOAM 相界面动态细化

`mesh.adaptivity dynamic` 在 `mesh.dynamicRefinement` 声明相分数字段 `field`、`refineInterval`、`nBufferLayers`、`maxRefinement`、`maxCells` 和细化、粗化阈值。串行 `interFoam` 从初始时刻运行原生 `dynamicRefineFvMesh`。已有 `constant/dynamicMeshDict` 与声明一致时使用原件，缺项时生成封存输入，冲突时拒绝运行及导出。平台核对网格变化事件、写出时刻的实际拓扑及各单元细化层级。标准包在独立目录使用相同网格原件和原生字典。 [OpenFOAM 动态网格](https://api.openfoam.com/2406/dynamicRefineFvMesh_8H.html) · [托管实现](../server/app/services/openfoam_dynamic_refinement.py)

间隔为 1 的双存储实算在 0.001 s 从 1728 增至 3072 个单元，六个时刻的 36 件原生场和拓扑与独立运行逐字节一致。相分数体平均约为 0.0729166663。间隔为 2 的实算保留 0.001 s 的 1728 单元，到 0.002 s 增至 3072，后续写出时刻保持 3072。E 确认、标准包独立复算和 Pandoc HTML／DOCX 均核对该结果。Edge 在「产物追溯」从所选求解版本读回 1728→3072 和变化时间。 [间隔 1 文件系统](../../tmp/ff-c10-udf-dynamic-refinement-k015rgna/dynamic-refinement.json) · [间隔 1 认证对象存储](../../tmp/ff-c10-udf-dynamic-refinement-rdhl3ntt/dynamic-refinement.json) · [间隔 2 文件系统报告](../../tmp/ff-c10-udf-dynamic-refinement-dywlgamv/dynamic-refinement.json) · [Edge](../../tmp/cb08-h18-edge-dynamic-refinement-64syb1na/browser.json)

间隔为 2 的认证对象存储复验也完成 B–E、报告与离线 `Allrun`。34 件原生场和拓扑与文件系统及独立运行一致，两次生成的报告分别核对下载摘要。对象存储首次间隔 2 在后处理发布失败，源求解已通过 D。 [认证对象存储复验](../../tmp/ff-c10-udf-dynamic-refinement-1tayzzue/dynamic-refinement.json) · [首轮对象存储](../../tmp/ff-c10-udf-dynamic-refinement-6pureb2y/dynamic-refinement.json)

## T02 验收

FF-CB09 已按 T02 汇总质量门禁、单元族、边界层、网格转换、原生自适应和报告。FF-B15 继续全类型结构单元确认。此组原生、存储及浏览器验收真实模型 0 次、模拟 0 次。 [T02 验收](../../tmp/FF-CB09-openfoam-dynamic-20261123/t02-validation.json) · [全功能台账](../../tmp/stage-ledger-evidence-20261123-ff-cb09.json)
