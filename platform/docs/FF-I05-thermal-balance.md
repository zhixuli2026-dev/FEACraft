# FloTHERM 求解后热平衡与监视器回查

`python platform/server/flotherm2foam.py balance case-output --report solved-balance.json` 核验已生成并完成求解的算例，也可省略 `--report` 并直接读取 JSON。已有报告文件受到保护。转换期的 `power-balance.json` 继续记录输入功率的映射覆盖。

当前求解后核验适用于由 ECXML 或 PDML 导入、填满正交计算域的单个各向同性固体。命令复核生成输入、实际网格、热源区、固定壁温和最新时刻的温度场。壁面功率按各面面积、所属单元温度和中心到壁面的距离积分，符号为向外散热为正。生成的纯导热算例使用这个结果提供壁面监视器。区域与探针继续使用 OpenFOAM 的 `volFieldValue` 和 `probes`，区域平均值另从当前温度场和实际体积回算。[OpenFOAM v2406 壁面热通量](https://api.openfoam.com/2406/classFoam_1_1functionObjects_1_1wallHeatFlux.html) · [OpenFOAM v2406 面法向梯度](https://api.openfoam.com/2406/snGradScheme_8C_source.html)

报告保存原始模型摘要、结果时刻、六个面各自的功率、原始温度和监视器路径及其 SHA-256。平衡容差取 0.01 W 与输入功率千分之一中的较大值，区域温度独立回算容差为 0.001 K。热不平衡和区域数据不一致分别报告原因。缺少最新时刻字段或监视器输出时，命令退出码为 2。静态检查按 JSON 内容比较对象，并核对文本求解字典的完整内容。

## 实测

| 项目 | 结果 |
| --- | --- |
| 求解与复核 | 非特权 WSL OpenFOAM v2406 求解 1,000 个单元，时刻 100。温度与独立离散回算最大差 0.000487 K。Windows 与 WSL 的报告一致。 |
| 热平衡 | 1,200 W 输入，实际外壁合计约 1,200.077 W，绝对差约 0.077 W。六个面各约 200.013 W。 |
| 监视器 | 原生区域平均温度 310.8026 K，选定探针 311.837 K。原始数据与温度场同一时刻。 |
| 故障检查 | 将最终温度场替换成均匀壁温时返回 2，报告热量差和区域温度不一致。缺失最新温度或监视器行时拒绝，已有报告内容保持。 |
| 后端兼容 | 认证 SeaweedFS 实际完成 ECXML 导入、源版本拒绝和原转换发布。 |

真实模型 0，模拟调用 0。[最终 OpenFOAM 验收](../../tmp/flotherm-heat-balance-final-20261120.json) · [认证 S3 复验](../../tmp/flotherm-heat-balance-s3-compat-20261120.json)

## 原始探针与区域

ECXML 的活动 `monitorPoint` 和 V2310 PDML 的活动探针可直接导入。导入保留探针名称、装配路径、全局米制坐标，以及 ECXML 元素位置或 PDML 记录偏移。同名探针需要位置唯一匹配，匹配后附带两份来源。三维固体归属无法确认的探针仍出现在覆盖报告中，并阻断直接求解。[JEITA 提供的 ECXML 导出实例](https://jeita-sdtc.com/download/AnualReport/2022/IEEE2401_TG/ECXML/20220715_ECXML_r0.pdf)

计算域和显式网格区域出现在 IR、算例清单及覆盖报告。PDML 旋转区域同时保留全局原点、三条有向边和外包范围。共轭传热算例可按区域 ID 启用局部体积细化。未选择的区域保留坐标。原始温度探针生成 OpenFOAM 原生 `probes`，其结果路径和 SHA-256 进入求解后报告。

| 实际检查 | 结果 |
| --- | --- |
| ECXML、PDML、两者配对 | 三组原始格式输入均转换并在非特权 WSL 的 OpenFOAM v2406 中实际求解，每组 1,000 单元。 |
| 探针回算 | 原始位置 (0.073, 0.091, 0.137) m，原生读数 311.837 K，独立回算约 311.836651 K。配对算例的 Windows 与 WSL 回查相同。 |
| 区域和阻断 | 斜置区域保留有向边。域外探针在报告中给出原因，禁止直接求解。同名不同位置的配对和缺坐标的 PDML 探针拒绝导入。 |
| 认证 SeaweedFS | 实际上传配对输入、导入 IR、发布算例和覆盖报告，保留四个区域记录和双源探针。 |

多区域共轭传热的探针归属和流道流量继续按 FF-I05、FF-I08 和 FF-I10C 的原范围推进。本轮真实模型 0 次，模拟调用 0 次。[原始探针验收](../../tmp/flotherm-native-monitors-20261120.json) · [热平衡兼容](../../tmp/flotherm-heat-balance-monitor-compat-20261120.json) · [七种导入组合](../../tmp/flotherm-paired-monitor-compat-20261120.json)

### 局部网格细化

通过 `import --config`、`build --config` 或工作台的设置 JSON 文件设置局部细化。以下示例假设 IR 中存在 `ecxmlRegion1` 和 `ecxmlSolid1`。独立 PDML 导入还需要计算域和环境温度。级别为 0–8 的整数，0 表示保留原网格。不同目标重叠时，OpenFOAM 按最高要求细化。纯导热的固定正交模板在预检中提示适用的共轭传热配置。

```json
{
  "boundaries": {"mode": "channel", "inletVelocity": 0.15},
  "refinement": {"ecxmlRegion1": 1},
  "objectOverrides": {"ecxmlSolid1": {"refinement": 1}},
  "maxSkewness": 2.0
}
```

结构化算例先生成原有材料区，再调用 OpenFOAM 的体积细化，保留原分界面。曲面算例在原 STL 网格流程中添加体积细化。PDML 斜置区域使用原点和有向边构造旋转盒。逐对象细化对选定实体的材料单元核验层级。网格门禁独立使用原生单元选择器和 `cellLevel`，要求选区含实际单元且全部达到所请求层级。超域请求在预检中拒绝。可用 `maxSkewness` 约束实测最大偏斜度。[OpenFOAM 体积细化](https://doc.openfoam.com/2306/tools/pre-processing/mesh/generation/snappyhexmesh/castellation/) · [OpenFOAM 旋转盒单元选择](https://api.openfoam.com/2406/classFoam_1_1rotatedBoxToCell.html)

| 非特权 WSL OpenFOAM v2406 | 实测结果 |
| --- | --- |
| ECXML 轴对齐区域和逐对象细化 | 区域 65 个单元和固体 128 个单元全部达到一级。输入 0.50 W，出口净显热约 0.499989 W。未启用细化的第二个区域保留坐标。 |
| V2310 PDML 旋转区域 | 924 个单元达到一级。旋转区域的外包盒中另有 152 个粗网格单元，验证了实际选择范围。输入 0.25 W，出口净显热约 0.249997 W。 |
| ECXML 圆柱曲面与区域叠加 | 区域 58 个单元达到一级，圆柱 448 个单元达到二级。输入 0.50 W，出口净显热约 0.499993 W。 |
| 独立风扇 | 风扇区 1,536 个单元达到一级，入口面区有 256 个面。原生求解读取体积流量约 0.00503997 m³/s。 |
| 拒绝条件 | 空选择和超域请求分别在实际网格检查与预检中拒绝。偏斜超限被原生网格质量检查拦住。纯导热路径保留原固定网格并提示细化配置的适用条件。 |

实际认证 SeaweedFS 发布了细化字典、区域清单和覆盖报告。原始探针的双源追溯、单固体及多材料流道通过复验。真实和模拟模型调用均为 0。[细化实测](../../tmp/flotherm-refinement-20261120.json) · [探针复验](../../tmp/flotherm-refinement-monitor-20261120.json) · [流道回归](../../tmp/flotherm-refinement-legacy-channel-20261120.json) · [纯导热预检](../../tmp/flotherm-refinement-pure-guard/result.json)

FF-I05 按原范围验收。ECXML 与 V2310 PDML 的 Monitor、Region、总功率源项和转换覆盖由真实输入、原生 OpenFOAM 结果及认证存储发布共同核对。原始探针缺坐标及同名异位被拒绝，缺少最新结果和热量不平衡会返回失败。当前 Edge 工作台复验通过输入导入、配置覆盖、打开覆盖报告、丢失提交响应后找回同一作业及取消后无输出发布。[工作台复验](../../tmp/flotherm-studio-browser-qz8xyfrh/browser.json) · [作业与产物回执](../../tmp/flotherm-studio-browser-qz8xyfrh/attestation.json)

## 同材质盒体合并与检查点续跑

通过工作台设置 JSON 或 CLI `build --config` 启用。以下共轭传热设置合并相连的盒体，并从最近的 OpenFOAM 检查点启动。

```json
{
  "boundaries": {"mode": "channel", "inletVelocity": 0.15},
  "mergeTouchingBoxes": true,
  "startFrom": "latestTime",
  "parallelSubdomains": 2
}
```

合并范围为同材质、具有共同面的正交盒体或同材质相交盒体。实际固体区使用盒体并集，重叠体积只计算一次。每个原始对象保留独立的单元选区、绝对源功率、已有温度监视器和细化范围。在 `case-manifest.json` 的 `merged_solid_regions` 和覆盖报告中可查成员、单元区及功率。薄板、PCB 和 2R 等专用模型保留各自的物理模型。

`startFrom` 支持 `startTime`、`latestTime` 和历史写法 `latest`，也可在 `openfoam` 对象内设置。纯导热支持续跑和 MPI 分解设置。启用续跑后，调整 `system/controlDict` 的 `endTime` 至晚于当前时刻，再执行 `bash Allrun.serial` 或 `bash Allrun.parallel`。串行入口在需要时先重建最新分区结果。并行入口复用同一分解布局，根目录的结果更新时只分发最新字段及控制器状态。已存在的历史根目录结果由 `reconstructPar -newTimes` 跳过。已有网格时，续跑先核验网格和检查点字段。并行分区时刻不一致、所需字段缺失或已有检查点时直接执行重新网格化均会在预检中停止。[OpenFOAM v2406 重建源码](https://api.openfoam.com/2406/reconstructPar_8C_source.html)

| 实际验收 | 结果 |
| --- | --- |
| 同材质并盒 | 相邻两个 ECXML 盒体合并为一个固体区。两个原对象的热源分别作用于 12 和 144 个单元，独立区域温度约 307.918 和 307.720 K。另验证相交盒的并集体积，以及关闭合并时的重叠拦截。 |
| 求解续跑 | 非特权 WSL OpenFOAM v2406 验证串行、双进程、两个方向的切换，以及只有分区时刻 1400 时重建并继续至 1600。旧检查点和网格逐文件校验通过。0.4 W 输入在出口测得约 0.399991 W 净显热。 |
| 纯导热与风扇 | 纯导热串行从 20 继续至 100，双进程二进制时刻 100 也通过热平衡。定流量风扇从串行切换到双进程后，控制器状态保留，实测体积流量约 0.00287998 m³/s。 |
| 平台入口 | 认证 SeaweedFS 上的实际转换作业发布了续跑脚本、检查点预检和独立对象的热源字典。Edge 工作台回归验证输入、配置、转换、取消和结果打开。 |

FF-I06 已验收。共轭传热接受顶层或 `openfoam` 中的 `parallelSubdomains`，同名项重复声明时数值必须一致。本轮真实模型 0，模拟 0。[OpenFOAM 与认证存储实测](../../tmp/flotherm-merge-restart-20261120.json) · [Edge 工作台](../../tmp/flotherm-studio-browser-7s674hac/attestation.json)

### 清理与重跑

求解结束后执行 `bash 路径/Allclean` 即可从任意工作目录清理该生成算例。脚本校验生成标记，移除识别到的求解时刻和 MPI 分区、生成网格、`postProcessing` 及普通 `log.*`，并将网格门禁恢复为待实跑。原始模型、`0` 初始场、物性、配置、转换报告及生成标记保留。重新执行 `Allrun.serial` 或 `Allrun.parallel` 会从清理后的算例开始求解。

纯导热和多区域共轭传热分别用非特权 WSL 的 OpenFOAM v2406 完成双进程求解、从父目录调用清理、重复清理和串行重跑。两组热平衡通过。验证了用户普通文件、同名式目录中的文字记录、日志符号链接及其外部目标均保留。[实际清理和重跑](../../tmp/flotherm-clean-20261120.json)

### G6 原版转换对拍

同一份 ECXML 与设置分别交给提取版 0.3.0 和平台版。生成侧逐文件记录 82 个路径的 SHA-256。两版求解器、分解方法和进程数一致，提取版默认使用二进制字段，平台版在该输入下使用 ASCII。两版的实际空气体积约 0.013038635 m³，固体总体积约 0.0001449585 m³。提取版按材料生成一个固体区，平台版保留独立盒体所在的第二个固体区。两版均生成 0.4 W 热源。

两版从时刻 0 求解至 200，再由已写结果继续至 2000。提取版的原生界面净热由约 0.048841 W 升至 0.290798 W。平台版在 2000 的界面净热约 0.400000 W，出口净显热约 0.399991 W。热量差异保留在对拍记录中，FF-I10 继续核对其余输入、语义差异及取消恢复。真实模型 0，模拟 0。[首个 G6 逐文件与数值对拍](../../tmp/flotherm-g6-reference-20261120/comparison.json)

第三组用相同 ECXML、网格设置和终点 2000 重新运行提取版，仅将能量松弛系数设置为 0.95 并启用严格能量求解。界面净热达到约 0.399990 W，与平台版相差约 0.0000103 W。[能量设置复验](../../tmp/flotherm-g6-reference-20261120/reference-tuning.json)

同一装配的七种离线组合均在两版实际求解到时刻 200，共 14 次。覆盖 ECXML、PDML、ECXML+PDML，以及 XTXML 或 XTXMLA 分别与 PDML、PDML+ECXML 的组合。每组记录 75 个路径的逐文件摘要，两版均映射 0.4 W 热源，空气和固体体积分别约 0.064 与 0.008 m³。七组的平台数值文件逐字节一致。提取版的数值文件差异为热源追溯 ID，两版各自的七组界面热量一致。认证 SeaweedFS 另验证了清理脚本和运行时的下载。FF-I10 继续核对长程物理与取消恢复。[七种输入的真实求解与逐文件摘要](../../tmp/flotherm-g6-reference-20261120/matrix/native-matrix.json) · [认证存储回读](../../tmp/flotherm-clean-s3-20261120.json)

## OpenFOAM 二进制结果

在转换配置中设置 `"writeFormat": "binary"`，或将同一个配置文件提交给 FloTHERM 转换 API 的 `settings_path`。旧算例和省略该设置的转换沿用原输出行为。实际求解后，`balance` 直接读取温度场、网格和热源单元选区的二进制数据，并保存每份原文件的 SHA-256。

```shell
python platform/server/flotherm2foam.py build model-ir --config binary.json --output binary-case
python platform/server/flotherm2foam.py balance binary-case --report solved-balance.json
```

读取器根据 FoamFile 声明核对字节序、标签和标量宽度，按精确长度拆出紧凑面偏移、面顶点和数值列表。对二进制坐标只合并浮点精度范围内的同一平面，随后校验正交六面体、边界面和热源区。当前交付范围为生成器对应的未压缩单固体算例及恒温壁。[OpenFOAM 紧凑面列表](https://api.openfoam.com/2406/MeshedSurfaceProxy_8C_source.html)

实际以非特权 WSL 运行 OpenFOAM v2406。相同源模型分别求解到时刻 100。两种格式各得到 1,000 个单元和 311.837 K 的探针温度。ASCII 外壁功率约 1,200.0768 W，二进制约 1,199.999526 W。二进制热平衡与输入 1,200 W 相差约 0.000474 W，Windows 与 WSL 回查一致。格式、截断列表、紧凑面偏移、负标签和变形网格的故障均被拒绝。

另从原生文件转换点、温度和所有者标签，验证大端序、单精度温度和 64 位标签的协议读取。相对原生结果的功率差约 0.0147 W，低于按单精度误差传播得到的约 0.0596 W。认证 SeaweedFS 的实际转换作业发布了二进制设置与清单。真实模型 0 次，模拟调用 0 次。[二进制算例与协议证据](../../tmp/flotherm-binary-balance-20261120.json) · [ASCII 回归](../../tmp/flotherm-binary-ascii-compat-20261120.json)

## 流道流量与原生壁面热流

已求解的 OpenFOAM 固定网格流道可用 `flow` 回查。默认识别唯一一对 `inlet/outlet` 或 `ambient_xMin/ambient_xMax`，也可通过 `--inlet` 和 `--outlet` 指定。命名区域通过 `--region` 选择。

```shell
python platform/server/flotherm2foam.py flow solved-case --region air --inlet inlet --outlet outlet --wall-heat-flux nativeHeat --report flow.json
```

命令对最新求解时刻的 `phi` 逐面求和，按字段量纲给出 m³/s 或 kg/s，符号为向外为正。报告包含各边界的进入量、流出量、净量和守恒差。其他边界存在超过容差的通过流、入口出口方向反接或总流量未闭合时，命令退出 2。报告保留原始字段和网格文件 SHA-256。较新的时刻缺字段时直接报错。[OpenFOAM face flux 汇总](https://doc.openfoam.com/2212/tools/post-processing/function-objects/field/surfaceFieldValue/)

选择 `--wall-heat-flux` 后，另读取同一时刻的原生壁面热流密度场。按实际网格面对积，回算每个原生记录壁面的最小值、最大值及总功率，与 `wallHeatFlux.dat` 比较。场文件默认 `wallHeatFlux`，可使用 `--wall-field` 指定。该回查使用求解器原生结果的符号。

实际在非特权 WSL OpenFOAM v2406 运行 `simpleFoam` 和 `chtMultiRegionSimpleFoam`。ASCII 不可压算例入口体积流量为 0.000254 m³/s，二进制 CHT 的入口质量流量约 0.413001 kg/s，两者的回算均与原生 `surfaceFieldValue` 一致。CHT 非零壁面功率约 806.169627 W，逐面回算与原生积分相差约 0.000000000043 W。Windows 和 WSL 的回查报告一致。反接入口出口、删去出口流量、侧壁漏流、错误量纲、原生报告改动及较新时刻不完整均已检查。[真实流道与原生壁面热流](../../tmp/flotherm-channel-wall-20261120.json)

## 由 FloTHERM 原始输入生成多区域流道

在导入或构建时提供下列配置。ECXML 使用原始计算域和环境温度。独立 PDML 另在导入配置中提供 `domain` 和 `ambient_temperature_k`。

```json
{
  "boundaries": {"mode": "channel", "inletVelocity": 0.15},
  "openfoam": {"baseCellSize": 0.02, "endTime": 2000, "writeInterval": 500},
  "writeFormat": "binary"
}
```

```shell
python platform/server/flotherm2foam.py import source.ecxml --config channel.json --output model-ir
python platform/server/flotherm2foam.py build model-ir --output channel-case
python platform/server/flotherm2foam.py check channel-case
bash channel-case/Allrun.serial
python platform/server/flotherm2foam.py flow channel-case --energy-balance --report channel-balance.json
```

运行脚本使用已加载 OpenFOAM 环境的 Linux 或 WSL。`blockMesh` 保留轴对齐固体的原始分界面，`splitMeshRegions` 将网格拆为一个空气区及每个固体各一个区域。每区通过 `checkMesh` 后才启动 `chtMultiRegionSimpleFoam`。空气沿正 X 方向进入，侧壁绝热、自由滑移。固体沿用原材料的导热率、密度和比热，每个固体的总功率作为该区的原生焓方程热源。全程使用 OpenFOAM 自带的热源和耦合边界。[OpenFOAM 总量热源](https://doc.openfoam.com/2312/tools/processing/numerics/fvoptions/sources/rtm/semiImplicit/) · [流固耦合边界](https://doc.openfoam.com/2306/tools/processing/boundary-conditions/rtm/derived/thermal/turbulentTemperatureRadCoupledMixed/)

清单记录各区域的材料、网格数、热源和探针来源。原始探针按坐标落入流体或固体，位于界面或域外的探针会阻断求解。每个区域写出原生温度极值，输出同时包含质量流量、流固表面热流及进出口质量加权温度。`flow --energy-balance` 用原生 `phi`、逐面重算的壁面热流和质量加权温度，核对源功率、界面热量和进出口显热。显热以入口环境温度为参照。报告列出两段差值、容差和输入结果文件的 SHA-256。此对账适用于由当前生成器生成、以进出口显热带走热量的稳态流道。

上面的结构化网格适用于计算域内部的轴对齐实心盒。风扇和阻力继续按原交付范围推进。

两个算例均在非特权 WSL 的 OpenFOAM v2406 实际运行，所有区域通过原生网格检查。ECXML 使用二进制结果和串行求解，PDML 使用 ASCII 结果和双进程 MPI，并用 `reconstructPar -allRegions` 重建。

| 原始输入 | 源功率 | 流固界面传热 | 进出口净显热 |
| --- | ---: | ---: | ---: |
| ECXML，两种固体材料并含空气探针 | 0.500000 W | 0.499985 W | 0.499997 W |
| V2310 PDML，含空气探针 | 0.200000 W | 0.200096 W | 0.198750 W |

ECXML 的 1,024 个单元分布在 3 个区域，固体和空气探针分别读到约 308.070 K 和 301.125 K。入口质量流量约 0.002541576 kg/s，与由入口面积、速度与理想气体密度算得的独立结果一致。界面探针和重叠固体被预检拦截。最新出口温度缺失时显热对账失败，原位恢复原生温度文件后可以再次回查。真实模型与模拟调用均为 0。[原始输入流道验收](../../tmp/flotherm-generated-channel-20261120.json) · [已有流道兼容](../../tmp/flotherm-generated-channel-existing-compat-20261120.json) · [单固体兼容](../../tmp/flotherm-generated-channel-conduction-compat-20261120.json)

认证 SeaweedFS 实际通过 API 导入和封存原始 ECXML，使用提交的流道配置完成转换。发布结果校验了两种固体、0.5 W 热源、运行前检查及文件清单。FF-I10C 已验收。FF-I08 和 FF-I10 的其余范围按台账继续。[交付台账](full-function-delivery-plan.md)

## 开放边界、realizableKE 与正交各向异性

使用 `boundaries.mode: open` 将六个外表面配置为环境开口。允许自然进出流，各开口在回流时采用原始环境温度。流体求解保留重力和浮力。`openfoam.gravity` 可覆盖重力矢量，单位为 m/s²。开放边界默认 `(0, -9.81, 0)`，定速流道沿用零重力。各开口写出原生质量流量并保留符号，向域外为正。外界压力使用减去动压的总压条件，随入流方向调整。[OpenFOAM 总压条件](https://doc.openfoam.com/2306/tools/processing/boundary-conditions/rtm/derived/inletOutlet/totalPressure/)

```json
{
  "boundaries": {"mode": "open"},
  "openfoam": {"turbulence": "laminar", "gravity": [0, -9.81, 0], "endTime": 2000}
}
```

`openfoam.turbulence: RAS` 选择 OpenFOAM `realizableKE`，也可直接填写 `realizableKE`。生成器为流体区写出 `k`、`epsilon`、`nut` 和 `alphat`，在固体界面使用原生壁面函数。`laminar` 保留为默认值。定速 `channel` 的入口仍需要 `inletVelocity`，开放边界的流量按六个开口分别记录。

ECXML 的 `thermalConductivity/orthotropic` 读取 `x/y/z`。PDML 读取对应的三个方向分量。IR 保存以 W/(m·K) 为单位的三个正数，按全局 X、Y、Z 主轴写入固体导热张量。每个固体独立使用原材料的密度、比热和导热率。流固界面沿面法向取张量导热率，确保固体求解和界面传热采用同一方向的数据。[OpenFOAM 固体热物性](https://api.openfoam.com/2406/heSolidThermo_8H_source.html)

FF-I10B 已通过非特权 WSL、OpenFOAM v2406 和认证 SeaweedFS 验收。

| 实际算例 | 核验结果 |
| --- | --- |
| ECXML 开放边界，层流 | 1 W 加热时最大流速约 0.09722 m/s，六个开口的净质量差约 5.2×10⁻¹² kg/s，流固界面积分约 0.99694 W。无热源对照的最大流速约 0.00273 m/s。 |
| ECXML 开放边界，realizableKE | 实际求解了 `k` 和 `epsilon`，湍流黏度为正。加热时最大流速约 0.08432 m/s，流固界面积分约 0.99900 W。 |
| ECXML、PDML 定速流道，realizableKE | 根据固体温度和实际网格距离独立回算三个方向的傅里叶热通量，并与流体侧逐面积分核对。输入导热率为 `(160, 12, 2)` W/(m·K)。ECXML 回算约 `(160.000022, 12.000000, 2.000000)`，PDML 约 `(159.717778, 11.998217, 1.999968)`，PDML 的最大方向相对偏差约 0.18%。 |
| 配置及存储 | 拒绝零导热分量和在开放边界请求固定流道监控。认证 SeaweedFS 的公开转换入口保留 `open` 配置和三个导热分量。七种导入组合与原始层流串行和 MPI 路径复验通过。 |

验证采用全局 X/Y/Z 导热主轴和域内轴对齐实体。省略边界配置时继续沿用原有单固体导热生成入口。真实模型和模拟调用均为 0。[主验收](../../tmp/flotherm-physics-20261120.json) · [ECXML 定速 RAS](../../tmp/flotherm-physics-ecxml-ras-20261120.json) · [ECXML 开放 RAS](../../tmp/flotherm-physics-open-ras-20261120.json) · [原始层流兼容](../../tmp/flotherm-physics-channel-compat-20261120.json) · [七种输入组合](../../tmp/flotherm-physics-paired-compat-20261120.json)

## 曲面与旋转实体的分区网格

ECXML 的 `solidCylinder` 使用原始包围尺寸和 `plane` 重建轴向与椭圆截面。V2310 PDML 读取圆柱半径、长度和轴向，并沿装配链变换到全局坐标。同一导入入口可重建 PDML 的旋转盒、三棱柱、四面体和反向四面体。IR 保留原始基点、三条有向边和全局包围盒。圆柱与棱柱的探针归属使用实体形状判断，位于外包范围内的空气点仍可测量流体温度。

向 `import --config` 或 `build --config` 传入 `boundaries.mode` 和 `openfoam` 设置即可生成共轭传热算例。含曲面或旋转实体时，转换生成闭合 STL 和 `system/snappyHexMeshDict`。`openfoam.surfaceRefinementLevel` 可设置表面细化级别，例如 `[1, 2]`。`Allrun.pre` 执行 `blockMesh`、`snappyHexMesh`、`splitMeshRegions` 和 `checkMesh`，核对每个区域和预期流固界面。曲面分区需要实体之间有流体间隙。所有实体均为正交轴对齐盒时沿用结构化网格。[OpenFOAM 多区域种子与细化](https://doc.openfoam.com/2312/tools/pre-processing/mesh/generation/snappyhexmesh/castellation/)

`case-manifest.json` 记录 STL 路径、三角面数、包围盒和解析体积。`mesh_gate.json` 记录网格实际单元数和体积。圆柱表面使用 48 个圆周分段。带热源的区域仍使用原始总功率，求解后可用 `flow --energy-balance` 检查各界面和进出口收支。2R、散热器、外壳及风扇阻力模型继续作为 FF-I04、FF-I08 的验收范围。

多曲面 PDML 算例首轮分区后出现偏斜超限，网格检查阻止了求解。收紧 `snappyHexMesh` 内部面和边界面的偏斜限制后，所有区域通过 `checkMesh`。曲面算例使用带限幅的非正交修正和固体焓松弛，提高非结构网格上的温度求解稳定性。

在非特权 WSL 的 OpenFOAM v2406 中，两个原始输入均完成真实网格生成、串行求解和二进制结果回查。`surfaceCheck` 确认每个实体表面闭合，且仅含一个连通分量。逐面重算界面热量，与原生壁面结果和进出口显热对账。

| 原始输入 | 区域和单元 | 解析体积对账 | 源功率、流固界面功率、进出口净显热 |
| --- | --- | --- | --- |
| ECXML 椭圆柱和实心盒 | 3 区，5,608 单元 | 圆柱体积差 0.51% | 0.500000、0.499999、0.499993 W |
| V2310 PDML 旋转装配，含 5 个实体 | 6 区，6,902 单元 | 旋转盒 0.04%、圆柱 0.36%、三棱柱 0.15%、四面体 2.91%、反向四面体 0.69% | 0.350000、0.350000、0.349973 W |

原生探针验证了圆柱内部和外包范围内的空气位置。同名同尺寸但轴向不同的 ECXML/PDML 圆柱配对被拒绝，相交实体在网格前被拦截。认证 SeaweedFS 通过实际 API 转换发布 STL、网格配置及清单。既有七种输入组合、结构化流道串行和 MPI 回归通过。本轮真实模型和模拟调用均为 0。[几何实测](../../tmp/flotherm-geometry-20261120.json) · [原输入兼容](../../tmp/flotherm-geometry-paired-compat-20261120.json) · [原流道兼容](../../tmp/flotherm-geometry-channel-compat-20261120.json)

## 平面薄板与等效 PCB

ECXML `solid2dBlock` 保留由 `plane` 指定的法向、面内尺寸、基点和材料。V2310 PDML 解析压平轴和低面、中面、高面的位置，并应用上级装配变换。IR 保存折算前的薄板平面。生成 OpenFOAM 算例时，从平面中心两侧等距构造有限厚度的固体区，按原材料和总瓦数求解。盒式结构网格沿板厚布置至少两层单元。

厚度优先使用逐对象 `objectOverrides.<id>.plateThickness`，其次使用源文件明确声明的 `thickness`，再使用 `openfoam.thinPlateThickness`。所有厚度均以米计。缺省时采用参考转换器的网格尺度等效值，即 `max(0.2 × baseCellSize, 10⁻⁷ × 域最大尺寸)`。清单同时保存原始三维尺寸、实际厚度、厚度来源、法向和建模体积，便于区分源文件参数和等效参数。

ECXML `printedCircuitBoard` 使用源文件声明的等效材料。V2310 PDML 可从基材、铜材料和已确认的铜体积分数模式计算等效层合材料。面内导热率按体积分数加权，厚向取加权调和平均。密度和体积热容也按体积分数加权，再由两者计算比热。PCB 的局部导热主轴跟随装配方向进入各向异性固体求解及流固界面。清单保存材料角色、记录偏移、铜含量和主轴。[OpenFOAM 固体各向异性导热](https://api.openfoam.com/2406/heSolidThermo_8H_source.html)

| 实测范围 | 结果 |
| --- | --- |
| ECXML 薄板和等效 PCB | 保留源文件 3 mm 厚度。板内探针 303.9847 K。输入 0.300000 W，出口净显热 0.29999948 W。 |
| V2310 PDML 薄板和层合 PCB | 从压平记录取中面，按配置生成 2.4 mm 厚的固体。4% 铜体积分数的 PCB 面内及厚向导热率约为 15.888 和 0.312490 W/(m·K)，厚向对应全局 Y。由固体温度梯度独立回算的三方向热量与流体侧约 0.18 W 的界面热量一致。整案出口净显热约 0.29999951 W。 |
| 倾斜 25° 的 V2310 PDML PCB | 材料轴随几何旋转。在 23 mm 背景网格配置下，0.18 W 输入对应约 0.181904 W 出口净显热，体积差约 0.04%。21 mm 配置触发网格门禁。20 mm 配置通过网格检查，但加长求解后界面功率仍偏离约 2.8%，触发热量门禁。 |
| 厚度覆盖与源文件配对 | 覆盖值 6 mm 优先于源厚度。缺省网格等效厚度 3.8 mm 及其来源可回读。ECXML 与 PDML 按薄板四角和 PCB 法向配对，法向冲突和超过 100% 的铜体积分数被拒绝。 |
| 认证存储与兼容 | 认证 SeaweedFS 公开转换接口保留源薄板厚度和 PCB 导热率。七种输入组合和曲面实体原生求解复验通过。 |

求解使用非特权 WSL 的 OpenFOAM v2406。真实模型和模拟调用均为 0。[薄板与 PCB 验收](../../tmp/flotherm-plate-pcb-20261120.json) · [缺省厚度](../../tmp/flotherm-plate-default-20261120.json) · [配对验收](../../tmp/flotherm-plate-paired-20261120.json) · [输入兼容](../../tmp/flotherm-plate-input-compat-20261120.json) · [曲面兼容](../../tmp/flotherm-plate-geometry-compat-20261120.json)

[斜放 PCB 验收](../../tmp/flotherm-oblique-pcb-20261120.json) · [斜放 PCB 网格与热量对比](../../tmp/flotherm-oblique-pcb-sensitivity-20261120.json)

## 2R 结到壳与结到板热阻

ECXML `twoResistorModel` 读取 `thetaJC`、`thetaJB`、总功率和安装方向。V2310 PDML 从确认为 2R 的紧凑器件记录读取同样的参数。配对时核对设备方向、两个热阻和总瓦数。转换按器件板侧方向选取法向平行、投影有交集的 PCB。先比较间距，再比较投影交集面积。等距且交集面积相同的候选会阻断生成。

壳侧端点使用器件外侧的等效壳体区，板侧端点使用所选 PCB 区。轴对齐的压平器件以原平面作为安装面，在远离 PCB 的方向构造等效壳体。生成器把原参考转换器的 `twoResistanceNetworkSource` 和编译配置放入算例。`Allrun.build` 使用 `wmake libso` 在算例内编译，串行和 MPI 入口都可加载该算例内的库。[OpenFOAM 有限体积源接口](https://api.openfoam.com/2406/classFoam_1_1fv_1_1option.html)

求解器根据两个区域的体积加权温度反复计算结温及两路功率。两路功率可以有正负，板侧负值表示热量从板侧进入器件网络。器件总功率计入功率清单一次。`two-r` 从最新时刻的原生分热记录、两侧原生平均温度和输入热阻独立回算结温及支路功率。`flow --energy-balance` 继续核对整个流道的界面与进出口收支。

```shell
bash channel-case/Allrun.serial
python platform/server/flotherm2foam.py two-r channel-case --report two-r.json
python platform/server/flotherm2foam.py flow channel-case --energy-balance --report channel-balance.json
```

非特权 WSL 的 OpenFOAM v2406 实测了原始 ECXML 串行求解，以及 V2310 PDML 两进程 MPI。原生分热日志、区域平均温度、壁面热流和进出口显热分别独立对账。

| 实际算例 | 结果 |
| --- | --- |
| ECXML 0.30 W 器件 | 壳侧约 0.132743 W，板侧约 0.167257 W，结温约 313.095286 K。PCB 自身另有 0.03 W，流道出口净显热约 0.329999 W。 |
| V2310 PDML 活动器件 | 两进程下壳侧约 0.151852 W，板侧约 0.148148 W，结温约 310.615980 K。 |
| 同一 PCB 上的无源器件 | 壳侧约 +0.013215 W，板侧约 −0.013215 W。实际存在板到壳的热传递，器件净注入功率约为 0 W。整个 PDML 算例出口净显热约 0.329998 W。 |
| 记录与输入故障 | 修改支路功率、删去最新支路、配对热阻冲突、负热阻、缺少 PCB、两块 PCB 等距等面积均被对应的检查拒绝。 |

认证 SeaweedFS 转换接口发布原始输入对应的热阻、配置、构建脚本及已登记的源码。压平的 ECXML 器件挂接 PCB 后也通过实际分热回查。既有薄板、PCB 和七种原始导入组合通过复验。本轮真实模型和模拟调用均为 0。[2R 实测](../../tmp/flotherm-two-r-20261120.json) · [多 PCB 歧义](../../tmp/flotherm-two-r-board-ambiguity-20261120.json) · [压平器件](../../tmp/flotherm-two-r-flat-20261120.json) · [薄板与 PCB 回归](../../tmp/flotherm-two-r-plate-compat-20261120.json) · [导入回归](../../tmp/flotherm-two-r-input-compat-20261120.json)

## P-Q 风扇、定流量风扇和流阻

ECXML `axial3dFan` 和 `rectangular2dFan` 支持原始带符号方向、P-Q 点和固定体积流量。P-Q 点的单位为 m³/s 和 Pa。原始 `hubSize` 留在 IR 中，当前轴对齐矩形体积源采用整片入口面积。V2310 PDML 识别风扇、定流量、流阻和穿孔板记录，支持按名称和几何与 ECXML 配对。单独使用 PDML 时，预检要求补足源文件中尚无法确认的物理参数。

转换通过 `topoSet -region fluid` 创建流体单元区和风扇入口面区。网格门禁对照结构化网格的理论单元数和面数。P-Q 风扇使用 OpenFOAM 原生 `fanMomentumSource`，在点间线性插值，超出范围采用端点值。定流量风扇跟踪按面密度换算的有符号体积流量，由算例内编译的 `fixedVolumetricFan` 调整压升。试验中的平均速度控制在开放边界下只达到目标入口流量的 69%，因此验收直接核对入口实际流量。流阻使用原生 `explicitPorositySource` 和 `DarcyForchheimer` 模型。[OpenFOAM DarcyForchheimer 体积源](https://doc.openfoam.com/2212/tools/processing/numerics/fvoptions/sources/rtm/explicitPorosity/)

如需补足或覆盖参数，可向现有 IR 的 `build --config` 提供以下按对象 ID 索引的配置。`import --config` 也读取同样的设备字段，独立 PDML 导入还需要计算域和环境温度。方向为带符号的全局单位轴向，`d` 和 `f` 分别为全局 X/Y/Z 分量，单位分别为 1/m² 和 1/m。`pqCurve` 和 `fixedFlowRate` 选择其一。薄流阻的体积系数需要原始厚度或显式等效厚度。清单记录参数、来源、入口面积、厚度及网格区。

```json
{
  "boundaries": {"mode": "open"},
  "fan": {"pdmlFlow35": {"direction": [-1, 0, 0], "fixedFlowRate": 0.00288}},
  "resistances": {"pdmlFlow180": {"coefficients": {"d": [1200000, 0, 0], "f": [400, 0, 0]}}}
}
```

`Allrun.serial` 和 `Allrun.parallel` 从生成的算例启动。风扇在写出时刻保存入口体积流量和源压升梯度到 `<time>/fluid/uniform/<source>Properties`。并行求解的原生状态位于 `processor*/<time>/fluid/uniform`。面区的 `surfaceFieldValue.dat` 同时保存流向一致的质量流量，可独立核对。

| 非特权 WSL OpenFOAM v2406 | 实测结果 |
| --- | --- |
| ECXML P-Q 低流量，含固体区 | 0.00504004 m³/s，源压升 14.960 Pa，跨设备探针压力差 14.946 Pa。 |
| ECXML P-Q 高流量 | 0.01079996 m³/s，源压升 7.800 Pa，跨设备探针压力差 7.809 Pa。 |
| ECXML 反向定流量，串行和双进程 | 目标 0.00288000 m³/s，两种运行的原生入口流量均为约 0.00287998 m³/s，独立的面区质量流量约 0.00338874 kg/s。 |
| V2310 PDML DarcyForchheimer 流阻 | 缺系数时预检阻断。补全系数后，6 mm 网格探针压降 3.729 Pa，均匀流公式参考值约 3.920 Pa，相差约 4.9%。15 mm 粗网格约 3.235 Pa。 |

当前风扇与流阻算例采用轴对齐的 Cartesian 设备区。与曲面 STL 网格组合会在预检中提示。轮毂实体遮挡、散热器和外壳仍属于 FF-I04 后续工程范围。实际认证 SeaweedFS 转换发布了定流量源文件、构建脚本和分区配置。原流道与 2R 原生求解复验通过。本轮真实模型与模拟调用均为 0。[流体设备原生及存储实测](../../tmp/flotherm-flow-devices-20261120.json) · [原流道兼容](../../tmp/flotherm-flow-devices-legacy-channel-20261120.json) · [原 2R 兼容](../../tmp/flotherm-flow-devices-legacy-2r-20261120.json)

## 散热器鳍片与密闭外壳

ECXML `heatsink/parts` 读取活动盒式零件、全局尺寸和材料。单一材料的零件合并为一个固体区域，部件名和边界仍保存在 IR。源文件在单一固体中的原始总瓦数计入一次。V2310 PDML 可从构造字段重建均匀板翅和共线矩形针翅，同时应用记录与装配坐标变换。ECXML 与 PDML 配对时逐个核对盒角，移动一片鳍片即使保持整个散热器的包围盒也会触发配对冲突。

ECXML 外壳读取 `wallThickness` 并生成六面壁。独立 PDML 外壳支持通过 `objectOverrides.<id>.wallThickness` 补充壁厚。清单保存壁厚来源、空腔位置和各自解析体积。结构化网格将鳍片实体、翅间空气、外壳壁及内外空气各自分区，并核对真实接口、单元数和体积。密闭空气区具有独立压力参考、无滑移壁和热耦合界面。探针分别绑定到鳍片、内部空气、外壳或外部流道。

密闭区域启用 OpenFOAM 的能量子迭代。初始分区求解在 1,200 步外壁只传出约 0.0294 W。一次隐式矩阵组装实验稳定在约 0.08943 W，偏差与两侧比热之比一致。正式算例采用能量子迭代后，第 600 步由外壳传入外部流道的热量约为 0.100000 W，出口净显热约为 0.098410 W。输入为 0.10 W，热流由壁面场和进出口结果独立回算。[OpenFOAM 能量子迭代说明](https://www.openfoam.com/index.php/news/main-news/openfoam-v1712/solver-and-physics)

| 实际验收 | 结果 |
| --- | --- |
| ECXML 四片鳍片和六面外壳 | 外部空气、密闭空气、外壳和散热器共 8,064 个单元。三个有界区域的网格体积与解析值一致。鳍片、翅间空气、外壳和外部空气探针依次约为 302.271、302.228、300.207 和 300.000 K。 |
| V2310 PDML 九根共线矩形针翅 | 网格保留针间空气。固体体积约 0.000156402588 m³，与底座加九根针的解析值一致。输入 0.10 W，出口净显热约 0.098602 W，通过既定热平衡门禁。 |
| PDML 外壳和跨格式配对 | 缺壁厚的 PDML 外壳进入待覆盖清单。显式壁厚补齐后，独立空气腔通过真实网格检查。ECXML 和 PDML 的板翅逐片通过几何配对，移动一片的输入被拒绝。 |
| 13° 斜放板翅 | 16.5 mm 背景网格通过未放宽的 `snappyHexMesh` 和 `checkMesh`，共 65,864 个单元，固体体积相对解析值偏差约 0.012%。原求解设置在 800 步时壁面净热量仅约 0.003430 W，触发热平衡门禁。保留原材料和网格，改用无欠松弛、收紧线性残差容差的固体焓求解后，从零启动第 320 步壁面净热量约 0.099997 W，出口净显热约 0.099904 W。 |

认证 SeaweedFS 通过实际导入和转换 API 发布密闭空腔、散热器及壁厚清单。原复杂基元与流体设备回归通过。当前密闭外壳采用轴对齐网格，曲面网格中的密闭组合和多材料鳍片的源格式支持继续核验。本轮真实模型与模拟调用均为 0。[散热器与外壳实测](../../tmp/flotherm-parts-final-20261120.json) · [斜放续算诊断](../../tmp/flotherm-parts-oblique-energy/convergence-result.json) · [原复杂基元](../../tmp/flotherm-parts-legacy-shapes-20261120.json) · [原风扇与流阻](../../tmp/flotherm-parts-legacy-flow-20261120.json)
