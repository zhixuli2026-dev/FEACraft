# FF-B15 网格质量预设

`mesh.qualityPreset` 为网格发布设置一组检查阈值。`qualityTargets` 可以覆盖其中任意阈值或指定原有的放行策略。工作流和 `mesh:evaluate` 都根据当前算例版本与实际 `checkMesh` 指标评估，验证日志和结果文件后才生成发布。产物追溯显示该次发布选用的预设。选择预设对算例生效，已有版本仍从原始完整回执读取。

| 预设 | 最大非正交角 | 最大偏斜度 | 最大长宽比 |
| --- | ---: | ---: | ---: |
| `smoke` | 80° | 4 | 100 |
| `coarse` | 75° | 4 | 80 |
| `production` | 70° | 4 | 60 |
| `meshIndependence` | 70° | 4 | 60 |

这些值为项目提供的默认阈值。缺少 `qualityPreset` 时直接使用声明的 `qualityTargets`。四种预设均默认 `fail`，并同时要求原生网格完整检查成功。可单独覆写上表的任一数值，保留其它预设阈值。`meshIndependence` 为后续比较的每一级使用同等质量要求。

真实 64 单元立方网格的 `checkMesh` 最长宽比为 1。`smoke`、`coarse` 和 `meshIndependence` 分别完成实际划分、评估、发布及 C 确认。生产预设覆盖长宽比上限为 0.9 时，实际网格的原生检查成功，项目发布门禁根据数值超限拦截在 B。无效预设返回 422，算例修订保持。 [冒烟网格](../../tmp/ff-b15-quality-preset-542hbagr/mesh-preset.json) · [粗网格](../../tmp/ff-b15-quality-preset-e6oj2dah/mesh-preset.json) · [网格无关性预设](../../tmp/ff-b15-quality-preset-s_1br1mg/mesh-preset.json) · [严格门禁负例](../../tmp/ff-b15-quality-preset-kl2vboaw/mesh-preset.json)

`production` 在 SQLite 文件系统和 PostgreSQL 认证 SeaweedFS 完成真实网格验证，均确认 C。对象存储把长宽比上限覆写为 24，保留最大非正交角 70° 等其余阈值。Edge 可在 `mesh` 块保存预设，并在「产物追溯」从所选发布查看预设及实际长宽比。首轮浏览器未能定位隐式关联的表单标签，添加明确的可访问名称后复验通过。 [本地浏览器及网格回执](../../tmp/ff-b15-quality-preset-ppur2ev1/mesh-preset.json) · [认证对象存储覆写](../../tmp/ff-b15-quality-preset-qbscnnb4/mesh-preset.json) · [Edge 保存](../../tmp/ff-b15-quality-preset-ppur2ev1/mesh-preset-edge-edit.json) · [Edge 查看](../../tmp/ff-b15-quality-preset-ppur2ev1/mesh-preset-edge-read.json)

四种来源 `generated`、`imported`、`solverNative`、`external` 及六种自适应枚举已有实际工作流证据。`none` 使用普通网格，`h`、`p` 和 `hp` 使用原生细化，`dynamic` 与 `goalOriented` 分别绑定 OpenFOAM 与 MOOSE。 [预设验收汇总](../../tmp/FF-B15-presets-20261123/validation.json) · [转换和自适应](FF-CB09-mesh.md)

生产预设也应用于导入的单区域 OpenFOAM 网格。普通和 gzip 原件、真实检查、64 单元导热和离线复算均已在双存储验证。 [单区域导入](FF-CB09-mesh.md)

## 网格无关性比较

同一算例可在「网格无关性比较」选择 3 至 8 个已发布的后处理版本，按名称选择有限标量指标并填写绝对、相对容差。接口 `POST /api/v1/cases/{case_id}/mesh/independence` 返回实际单元数、指标、量纲、各级发布 ID、相邻两级差值和容差。绝对与相对容差叠加为 `绝对容差 + 相对容差 × |较细网格指标|`。最细两级的判断显示在结果表上方，完整 JSON 可下载。 [比较服务](../server/app/services/mesh_study.py) · [算例界面](../web/src/panels/CaseMeshStudy.tsx)

当前比较支持标准 `blockMesh`、注册串行 OpenFOAM 求解和同一后处理定义。每一级使用封存的网格、求解和后处理版本。原生网格摘要除去六面体块的划分数量后，仍覆盖坐标、连接、缩放、边界及 grading。比较核对原生字典与该次求解的文件摘要，并要求各级几何、物性、求解设置、参考定义和量纲一致。没有此项几何证明的历史版本可重新划分并完成后处理，取得含新证据的发布。 [原生几何摘要](../server/app/services/mesh_study_geometry.py)

本地文件系统五次实际工作流均经过 B、C、D、E。64、216 和 512 单元的前三次通过容差 `10⁻⁶ K`，最细两级平均温度差 `2.483524212948396 × 10⁻⁷ K`，使用 `10⁻⁹ K` 时结果显示超差。Edge 下载与比较接口结果一致。随后用 1000 单元更换扩散系数、用 1728 单元改变原生网格比例，两次真实结果均被比较接口拒绝。重复版本、缺指标和越权读取也已验证。 [文件系统工作流](../../tmp/ff-b15-mesh-independence-6abe2e74/mesh-independence.json) · [Edge 下载](../../tmp/ff-b15-mesh-independence-6abe2e74/mesh-independence-browser.json)

PostgreSQL 与认证 SeaweedFS 的三个版本同样完成 B 至 E，实测指标、差值和两个容差判断均与本地组合一致。初次双组合实算已产出三个版本，首轮测试把 `10⁻⁹ K` 当成正例，导致断言失败。复测使用原解析基准的 `10⁻⁶ K`，并额外对 `10⁻⁹ K` 验证超差结果。 [认证对象存储工作流](../../tmp/ff-b15-mesh-independence-12xz7r7f/mesh-independence.json) · [首次文件系统记录](../../tmp/ff-b15-mesh-independence-3wq86f_c/mesh-independence.json) · [首次对象存储记录](../../tmp/ff-b15-mesh-independence-b641do16/mesh-independence.json)

## CalculiX 结构单元族

固体算例的 `mesh.elementFamilies` 与 CalculiX 输入和 `*INCLUDE` 中的原生 `*ELEMENT,TYPE=` 一起封存。声明必须覆盖实际使用的类型。原生输入、包含文件与选定算例修订随同一尝试核验，成功发布在「作业与日志 → 求解成果 → 结构单元」显示单元族及类型。OpenFOAM 的单元族继续按真实几何维度及 `checkMesh` 的原生统计核验。 [CalculiX 2.23 单元目录](https://dhondt.de/ov_calcu.htm) · [封存实现](../server/app/services/calculix_artifacts.py) · [OpenFOAM 单元统计](FF-CB09-mesh.md)

原生试件包含桁架 `T3D2`、梁 `B31`、三角 `CPS3`、四边 `CPS4`、壳 `S4`、四面体 `C3D4`、棱柱 `C3D6` 和六面体 `C3D8`。双存储八个测点均为 10 mm，符合解析应变和独立 CCX 输出，两套托管 DAT 逐字节一致。声明去掉梁时，提交被拒绝，旧发布和 DAT 仍可读。Edge 在 1440、900、640 像素窗口用键盘选择原作业，核对版本和下载摘要。 [独立原生](../../tmp/FF-B15-structural-families-20261123/native/reference.json) · [文件系统](../../tmp/ff-b15-structural-families-z0ejj7oh/families.json) · [认证对象存储](../../tmp/ff-b15-structural-families-aqdoaqdk/families.json) · [Edge](../../tmp/cb08-h18-edge-structural-families-qkd10a38/browser.json)

FF-B15 按 T03 验收。前端构建写入 D 盘，C 盘可用空间由 17,694,478,336 B 变为 17,694,330,880 B，Docker 内部回收 0 B。此专项真实模型和模拟均为 0 次。 [T03 验收](../../tmp/FF-B15-structural-families-20261123/t03-validation.json) · [构建和磁盘记录](../../tmp/FF-B15-structural-families-20261123/build-disk.json)
