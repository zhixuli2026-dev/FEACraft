# 原生 ParaView 插件（FF-D20）

在「可视化 → ParaView 来源与过滤器 → ParaView 插件」选择当前环境登记的插件。后端在独立 ParaView 进程内装载所选项，界面读取程序返回的版本、依赖、原生条目及属性定义。已经接入的条目可直接交给当前算例的受管管线。管线会在自己的原生进程内重新装载所选插件，并把实际装载记录写入 `pipeline.json`。

`GET /api/v1/cases/{case_id}/paraview/plugins` 返回实际登记和默认装载状态。`GET /api/v1/cases/{case_id}/paraview/plugins/{name}` 返回独立探测结果及可用管线条目。执行 `POST /api/v1/cases/{case_id}/paraview/pipeline` 时传 `plugins` 数组。算子使用原生 XML 名称，属性名称保留 XML 中的空格。

## 版本和装载矩阵

| 运行环境 | 登记 | 实际装载核对 | 默认 |
| --- | ---: | ---: | --- |
| Windows ParaView 5.11.2 | 28 | 28/28 | `vtkPVInitializerPlugin` 随进程启动，其他 27 个为按需装载 |
| Linux ParaView 5.11.2 OSMesa | 32 | 32/32 | `vtkPVInitializerPlugin` 随进程启动，其他 31 个为按需装载 |

Windows 的 28 来自程序插件登记，包含内置初始化器。详细清单包含同名插件的版本、默认状态、装载后依赖和新增代理。Linux 包含不同的额外插件。库提供的渲染表示和交互能力依安装包构成变化。 [Windows 明细](../../tmp/FF-D20-paraview-plugins-20261123/windows/matrix.json) · [Linux 明细](../../tmp/FF-D20-paraview-plugins-20261123/linux/matrix.json) · [ParaView 5.11 显示能力](https://docs.paraview.org/en/v5.11.2/UsersGuide/displayingData.html)

首批算例 API 在 Windows 和非特权 Linux 都验证了 ParametricSurfaces 的 480 点/960 单元、NonOrthogonalSource 的 9,261 点/40,000 单元，以及 `pvbatch` 中 AcceleratedAlgorithms 的 456 点/884 等值面单元。Edge 在 1440、900、390 px 修改实际曲面参数并生成结果。 [Windows 管线](../../tmp/FF-D20-paraview-plugins-20261123/managed-plugins-win32.json) · [Linux 管线](../../tmp/FF-D20-paraview-plugins-20261123/managed-plugins-linux.json) · [Edge 验收](../../tmp/ff-d20-plugin-edge-o4ovqoq8/plugin-browser.json)

文件节点可通过 `reader` 明确选择已装载插件提供的源。NIfTI 实际读取 64 点和 27 单元，GMV 与 Legacy Exodus 各读取同一四面体的 4 点、1 单元。Windows GMV 需要按运行时传原生绝对路径，已在私有作业内修正。测试使用通过算例正常上传的原始文件，发布输出通过原生 VTK 写入。 [双平台数据读取回执](../../tmp/FF-D20-paraview-plugins-20261123/managed-plugins-win32.json)

后续将九个工作流接入受管算例作业，并在 Windows 与 Linux 使用实际数据验收。包括 ArrowGlyph、DigitalRockPhysics 的簇统计和炸开、GeodesicMeasurement 的路径和距离字段、Moments、SLACTools、ExplicitStructuredGrid 和 HyperTreeGridADR。Windows 和 Linux 都读回了原生数据。Linux 的 SLAC 采样在 MPI 初始化时出现缓冲区别名错误，串行 `pvpython --no-mpi` 后为 289 点、256 单元。 [Windows 受管验收](../../tmp/FF-D20-paraview-plugins-20261123/managed-plugins-extended-win32.json) · [Linux 受管验收](../../tmp/FF-D20-paraview-plugins-20261123/managed-plugins-extended-linux.json)

2026-11-24，Windows 和非特权 Linux 容器都通过 XFEM 裁切、分层加厚和粒子源的受管作业与原件回读。两端的裁切单元都位于 `x ∈ [0, 0.5]`，合计体积 0.5。ParaView 结果文件保留未引用顶点，因此几何验收按实际单元包围盒计算。楔体厚度从 0.10 变为 0.25，体积为 0.125。粒子源发布 2 层、9 块和 144 个点。缺少 XFEM 切面数组的输入返回作业失败。Linux 使用和生产 Compose 相同的 seccomp 配置，临时文件写在容器原生文件系统中。 [Windows 回执](../../tmp/ff-d20-engineering-plugins-ci24o6al/engineering-plugins.json) · [Linux 回执](../../tmp/ff-d20-engineering-linux-20261124/results-seccomp-20261124/ff-d20-engineering-plugins-5q8fix42/engineering-plugins.json)

`ExtractBagPlots` 可接收算例上传的 `.vtt` 表格。工作台从真实行数据列读取待选变量，提交 `SelectArrays` 后以 `result.vtm` 发布数据。Windows 和非特权 Linux 均输出 24 行函数统计、12 条投影观测、6 行阈值和 24 × 24 的密度网格，并渲染 900 × 560 的原生函数 Bag Plot。Edge 以 1440 和 390 px 验证图像预览与下载。浏览器下载和 Windows 发布回执的 SHA-256 一致。 [Windows 图表与数据](../../tmp/ff-d20-engineering-plugins-292xk08f/engineering-plugins.json) · [Linux 图表与数据](../../tmp/ff-d20-chart-tracking-linux-20261124/results/ff-d20-engineering-plugins-spv7txh5/engineering-plugins.json) · [Edge 回执](../../tmp/ff-d20-bag-plot-edge-gghpoh4p/bag-plot-browser.json)

`LagrangianParticleTracker` 在工作台接受流场、粒子种子和表面三个输入，沿用原生 Matida 和 Runge Kutta 2 默认设置。三份 Kitware 测试样例按发行版的 SHA-512 校验。Windows 与非特权 Linux 发布了 100 条轨迹、2,873 个点，逐条检查种子编号、递增积分时间及有限的三维速度。当前工作流发布轨迹端口。碰撞等附加输出和种子、表面辅助器继续验收。 [Windows 轨迹](../../tmp/ff-d20-lagrangian-7_kspdvk/lagrangian.json) · [Linux 轨迹](../../tmp/ff-d20-chart-tracking-linux-20261124/results/ff-d20-lagrangian-etqj2z78/lagrangian.json) · [Kitware 5.11.2 测试样例与哈希](https://gitlab.kitware.com/paraview/paraview/-/tree/v5.11.2/Plugins/LagrangianParticleTracker/Testing)

Datamine 线框读取接受同目录的 `名称pt.dm` 点文件与 `名称tr.dm` 拓扑文件。选择点文件启动即可，原生读入器会从冻结算例输入查找同名拓扑，并发布全部六个可用单元字段。Kitware 的双文件样例在两个平台都发布了 60,942 个点和 155,378 个三角面，且每个单元字段均包含 155,378 个值。 [Windows 线框](../../tmp/ff-d20-engineering-plugins-peest_yo/engineering-plugins.json) · [Linux 线框](../../tmp/ff-d20-datamine-final-linux-20261124/results/ff-d20-engineering-plugins-9t7dejz0/engineering-plugins.json) · [Kitware 5.11.2 样例](https://gitlab.kitware.com/paraview/paraview/-/tree/v5.11.2/Plugins/Datamine/Testing/Data)

Surface LIC、Stream Lines、Bump Mapped Surface 和 Extrusion Surface 已接入受管预览。插件条目选择真实数据和相应的三分量矢量或单分量标量，发布 `inspection.png` 和原生状态。检验读取原生回执中的实际显示类型与字段，四种模式在 Windows 和非特权 Linux 都产生了与普通表面不同的图像。Edge 在 1440 和 390 px 完成 Surface LIC 的场选择和结果预览。 [Windows 四类显示](../../tmp/ff-d20-engineering-plugins-e522dcxx/engineering-plugins.json) · [Linux 四类显示](../../tmp/ff-d20-representation-linux-20261124/results/ff-d20-engineering-plugins-wfvc8ene/engineering-plugins.json) · [Edge 验收](../../tmp/ff-d20-representation-edge-w29u_j6h/representation-browser.json)

PanoramicProjectionView 在插件处选择等距圆柱或鱼眼投影，角度支持 90–360°。双端实际渲染 1024 × 512 的 180°、360° 两张等距圆柱图与 800 × 800 的 360° 鱼眼图，核对了角度变化、圆外遮罩、场景像素、尺寸和原件哈希。Edge 修改到 270°，在桌面和移动宽度预览并下载。相机位于输入的包围盒中心，视轴默认从等轴方向选取。 [Windows 全景](../../tmp/ff-d20-engineering-plugins-95brn2ff/engineering-plugins.json) · [Linux 全景](../../tmp/ff-d20-panorama-linux-20261124/results/ff-d20-engineering-plugins-oa1ls3sg/engineering-plugins.json) · [Edge 全景验收](../../tmp/ff-d20-panorama-edge-wj7mdoit/panorama-browser.json)

工作台的「管线编辑器」支持为来源或算例文件添加节点、连接多个分支和编辑原生参数。Windows 的 NodeEditor 条目可直接打开同一编辑器。Edge 在 1440 和 390 px 核对了四节点管线的前驱关系、真正的网格、455 个点、893 个单元和发布的原生 `.pvsm` 状态。 [Edge 图连接和原生结果](../../tmp/ff-d20-node-editor-2bbcd1qo/pipeline-editor.json)

LANLX3DReader 接入 `.x3d`。验收输入按 ParaView 5.11.2 的定宽文本格式构造单位四面体，Windows 和非特权 Linux 使用原生读入器发布 VTK 多块原件，回读确认四个顶点、一个体单元、体积 1/6、材料编号、293.15 的单元标量和四个点矢量。验证范围为单分区三维输入。 [Windows X3D](../../tmp/ff-d20-engineering-plugins-g1v1z5ct/engineering-plugins.json) · [Linux X3D](../../tmp/ff-d20-lanl-linux-20261124/results/ff-d20-engineering-plugins-jlb7gbfs/engineering-plugins.json) · [Kitware 原生格式定义](https://gitlab.kitware.com/paraview/paraview/-/tree/v5.11.2/Plugins/LANLX3DReader/Reader)

AnalyzeNIfTIReaderWriter 的写入条目接受 VTI、NIfTI 或 Analyze 体数据。NIfTI 输出单文件 `.nii`。Analyze 发布 `.hdr` 和 `.img`，应成对保存。双端原生读回 4 × 4 × 4 的 64 个体素。NIfTI 保持三轴不同间距，Analyze 按此版本能力采用等间距单分量输入。非等间距的 Analyze 请求明确失败，已发布原件仍保持原哈希。Edge 实际下载两件原件并核对哈希。 [Windows 体数据](../../tmp/ff-d20-native-volumes-33aezrmz/native-volumes.json) · [Linux 体数据](../../tmp/ff-d20-volume-linux-20261124/results/ff-d20-native-volumes-2kx10hux/native-volumes.json) · [Edge 原件下载](../../tmp/ff-d20-volume-edge-kara3frg/volume-browser.json)

SaveStateAndScreenshot 和 FlipBookPlugin 的入口在管线编辑器里选择输出。截图和 `.pvsm` 同作业发布并可分别下载。对照播放在同一相机下逐帧显示所选节点，可用空格切换、调节播放速度和循环播放。Edge 实测打开两个插件条目、下载匹配哈希的原件以及用空格和自动播放切换两张真实原生帧。Web 管线编辑器也可直接选择相同输出，适用于缺少 Qt 菜单插件的 Linux 安装。 [状态和对照播放](../../tmp/ff-d20-node-editor-5om1nt60/pipeline-editor.json)

StreamingParticles 支持从 Random Points 源生成分块预览。ParaView 5.11.2 在当前独立进程中计算并暂存原生块，工作台使用这些真实块累积点云并渲染每一步。复验每步加入 16 点，9 个不同的原生块和图像从 16 点累积到 144 点。Edge 在 1440 和 390 px 从第一帧跳到最后一帧。这一入口按固定相机生成受管预览，默认最多截取 16 步。 [Windows 粒子分块](../../tmp/ff-d20-engineering-plugins-iah2e7lb/engineering-plugins.json) · [Linux 粒子分块](../../tmp/ff-d20-streaming-final-linux-20261124/results/ff-d20-engineering-plugins-l_s5g7bz/engineering-plugins.json) · [Edge](../../tmp/ff-d20-node-editor-s03x54k_/pipeline-editor.json)

FF-D20 按 T06 通过。矩阵列出原 Windows 发布版的 28 个登记项及各自验证过的工作流。Linux 安装包的 32 个登记项均通过实际装载，Qt 菜单相关工作流可从 Web 管线编辑器使用。普通属性关键帧回归检查半径 0.4、0.6、0.8 的三张原生帧。实际模型 0 次，模拟 0 次。 [逐插件验收与原件哈希](../../tmp/ff-d20-final-acceptance-20261124/t06-plugin-coverage.json) · [关键帧回归](../../tmp/ff-d20-engineering-plugins-p9ohbls2/engineering-plugins.json)
