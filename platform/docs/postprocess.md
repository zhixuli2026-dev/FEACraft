# 后处理指标与数值证据（WP06）

持久工作流后续接线：postprocess.run消费不可变求解/网格与独立参考，实际重算指标/图件并绑定E确认；现有运行和结果API的注册/兼容分支及报告门禁见 [postprocess-workflows.md](postprocess-workflows.md)。本文原数值算子说明和已验收范围继续适用。

多序列出版图支持叠加、分面、误差带、可回算 CSV 和自动报告插图。版本下载及 `caseDict` 配置见[出版级图表](FF-D23-publication-charts.md)。

指定 OpenFOAM 边界后可以计算面积分和带符号的法向通量。全场 POD 支持体积加权和按发布下载 VTU。配置见[边界与模态](FF-D11-field-extraction.md)。

已有 `D-post/postprocess.py` 可以从作业面板在 WSL 沙箱运行。用户生成的 PNG/JPEG 和对应 CSV 可配置到自动组稿。用法见[脚本与报告图件](FF-D14-script-report.md)。

> 当前实现：2026-10-26 垂直切片。面向需要配置、验证或扩展后处理链的开发/维护者。
> 事实源仍是 `0-caseDict/caseDict`；`.vibeflow/jobs/postprocess-*-config.json` 只是提交时的不可变执行快照，不是第二套手工参数源。

## 1. 当前能力

- 从真实 OpenFOAM `internalMesh` 读取各时刻场，使用 VTK 做 `pointProbes`、`lineSamples`、整体表面积分和体平均。可在指定边界做面积分和法向通量。
- 对点探针时间序列计算 rFFT 频谱和时间 POD/SVD 模态。可对同一内部网格的场数据计算体积加权 POD，并导出空间模态 VTU。
- 读取同网格 OpenFOAM uniform/nonuniform List 参考场，计算体积加权 L2 并追溯参考文件 hash。
- 生成标量指标、AST 白名单公式、参考值 L2 对比、基础不确定度、comparisonTargets 类型链、单位/来源检查和 `readiness`。
- 输出 CSV、JSON、输入/产物 hash 和 artifact manifest。
- 从探针/线采样生成带单位、Okabe–Ito 配色和来源脚注的 PDF/SVG/300–600dpi PNG（Agg）。
- 每次成功作业写入独立版本目录；失败作业不覆盖最近一次有效结果。
- 结果支持本地文件系统和 S3 发布与读取。

前次运行指标可从同算例已发布版本按 ID 自动取数，运行清单记录源指标摘要。NOAA 小时实测可自动采集，通用 CSV 可以按时间、单位和垂直基准对齐。异网格 VTU 可按目标单元中心取参考单元值或点插值。TMR 风洞档案可自动采集，按原始观测坐标与求解剖面逐点比较，并将来源、容差、误差和档案摘要带入正式报告。 [前次运行引用](FF-D12-previous-reference.md) · [公开观测](FF-D12-observations.md) · [异网格与实测剖面](FF-CB12-reference-grid.md) · [数组公式](FF-D12-array-formulas.md) · [标量不确定度传播](FF-D12-uncertainty.md)

## 2. caseDict 配置

在 `postProcessing` 中配置。`fields` 是当前用于声明场单位的扩展；`runDir` 默认 `C-run/case-1`，也可由运行请求覆盖。

```text
postProcessing
{
    objectives
    {
        comparisonTargets
        {
            formulaBenchmark
            {
                metric p_times_two.value;
                reference formulaCheck;
                type benchmark;
            }
        }
    }
    runDir "C-run/case-1";
    fields
    {
        p { unit 1; }
        U { unit m/s; }
    }
    dataExtraction
    {
        pointProbes
        {
            probes
            {
                fields (p U);
                // 扁平 xyz 序列；本例为两个探针
                points (0.5 0.5 0.5 0.25 0.25 0.25);
            }
        }
        lineSamples
        {
            centerline
            {
                fields (p);
                start (0 0.5 0.5);
                end (1 0.5 0.5);
                samples 5;
                time 0.3;       // 省略时使用最后时刻
            }
        }
        surfaceIntegrals
        {
            boundary_p { field p; time 0.3; }
        }
        volumeAverages
        {
            volume_p { field p; time 0.3; }
            volume_U { field U; time 0.3; }
        }
        frequencySpectra
        {
            p_spectrum
            {
                source probes;  // pointProbes 中的操作 ID
                field p;
                component 0;
                detrend true;
            }
        }
        modalQuantities
        {
            p_pod
            {
                source probes;
                field p;
                component 0;
                rank 2;
                detrend true;
            }
        }
    }
    derivedQuantities
    {
        referenceFields
        {
            p_reference
            {
                field p;
                path "Ref/p-verification";
                time 0.3;
                kind verification;
                source "manufactured uniform reference field";
                l2_max 1e-12;
            }
        }
        formulas
        {
            p_times_two
            {
                expression "volume_p * 2";
                inputs { volume_p "volume_p.p@0.3"; }
                unit 1;
            }
        }
        uncertainty
        {
            p_formula_uncertainty
            {
                metric p_times_two.value;
                standard 0.01;
                coverage 2;
                source "manufactured standard uncertainty";
            }
        }
        referenceValues
        {
            volumeAt03
            {
                metric "volume_p.p@0.3";
                value 1;
                unit 1;
                kind verification;
                source "manufactured uniform p(t=0.3)=1";
                l2_max 1e-12;
            }
            formulaCheck
            {
                metric p_times_two.value;
                value 2;
                unit 1;
                kind verification;
                source "manufactured formula check";
                l2_max 1e-12;
            }
        }
    }
    artifacts
    {
        plots
        {
            probe_p
            {
                source probes;       // 也可填 lineSamples 的操作 ID
                field p;
                component 0;
                probe 0;
                title "Manufactured probe pressure";
                xLabel Time;
                yLabel Pressure;
                formats (pdf svg png);
                dpi 300;
            }
        }
    }
}
```

规则：

- 操作 ID 仅允许字母、数字、`_`、`-`，最长 80 位；必须唯一使用产物和指标命名空间。
- `points` 是扁平 xyz 序列；执行器快照中转换为三维点。线采样点数为 2–1001，探针数最多 256。
- `pointProbes` 默认读取全部时刻；可用 `time` 或 `times` 限定。线采样和积分默认最后时刻。
- 频谱要求至少 4 个等间隔递增时刻。POD 可以使用探针序列，或用 `sourceType cellField` 选择同网格的多个时刻。
- 参考键可任意命名，但 `metric` 必须指向实际指标。含 `@`、`^` 等字符的值应按 caseDict 字符串语法加引号。
- 图表 `source` 指向探针或线采样；探针图 x 默认时间 `[s]`，线图 x 默认距离 `[m]`，可用 `xUnit/xLabel/yLabel` 覆盖。`dpi` 限 300–600，格式只允许不重复的 pdf/svg/png，缺单位会拒绝生成出版图。
- 公式 `inputs` 把表达式变量映射到标量指标，结果名为 `{formula_id}.value`，可继续被其他公式或 referenceValues 引用。只允许数值常量、变量、四则/乘方/取模、正负号和 `sqrt/abs/min/max/log/exp`；不执行 Python eval/exec，不允许属性、下标或任意函数调用。
- `uncertainty` 的基本配置为 `metric/standard/source`，`coverage` 默认 2。标量公式可声明输入标准不确定度，并提供有来源的显式协方差矩阵。缺来源、指标或非法数值会进入 readiness blockers。[配置及实际验收](FF-D12-uncertainty.md)
- `referenceFields` 指向 case 内 OpenFOAM 场文件，支持 `internalField uniform` 和 `nonuniform List<scalar/vector>`；uniform 可广播到全部单元，非均匀列表必须与当前网格单元数/分量一致。路径越界、shape 不符、缺来源或超容差均不能形成通过证据。
- 异网格参考场可用 `referenceFields` 的 `mode cellCenters` 引用封存的 VTU 体网格。选择 `referenceAssociation cell` 或 `point`，并声明与目标一致的 `referenceUnit`。所有目标单元中心必须有有效参考值。实际输出记录源网格大小和体积加权 L2，可在比较目标和报告中使用。 [异网格配置及验收](FF-CB12-reference-grid.md)
- `comparisonTargets` 通过 `metric/reference/type` 链接目标，`benchmark` 对应 `verification`，`experiment` 对应 `validation`，`previousRun` 和 `requirement` 对应 `regression`。reference ID 不存在、指标缺失或类型错配都会进入 blockers。前次运行指标和公开观测均可通过现有参考值配置引用。[前次运行](FF-D12-previous-reference.md) · [公开观测](FF-D12-observations.md)

## 3. 数值定义

| 操作 | 当前定义 |
|---|---|
| pointProbes | VTK cell-to-point 后的线性插值；域外点通过 valid mask 拒绝 |
| lineSamples | start/end 间等距点，输出累计距离和各场值 |
| surfaceIntegrals | 省略 `patch` 时以整网格外表面和邻接单元值计算面积分。指定 `patch` 后可使用边界值积分，或以 `normalFlux` 计算向量的带符号法向通量 |
| volumeAverages | `sum(cell_value * cell_volume) / sum(cell_volume)`；向量逐分量 |
| frequencySpectra | 可选去均值的 rFFT；幅值按 `2/N * abs(FFT)`，DC/偶数 Nyquist 单独减半 |
| modalQuantities | 探针 POD-SVD 或全场体积加权快照 POD，返回奇异值、能量比例、时间/空间模态 |
| referenceFields | 同网格逐单元比较，绝对范数 `sqrt(sum(volume*(actual-reference)^2))`，相对范数再除以 `sqrt(sum(volume*reference^2))`；零参考且误差非零时 relative 为 null |
| formulas | AST 白名单逐节点解释；表达式最长 500 字符/100 节点，指数绝对值限 12，输入和结果必须为有限标量 |
| uncertainty | `expanded=standard*coverage`。传播配置通过局部灵敏度 `J` 和协方差 `Σ` 求 `standard=sqrt(J Σ Jᵀ)`，单位、来源和计算证据随指标保存。 |
| reference L2 | `sqrt(sum((actual-reference)^2)) / sqrt(sum(reference^2))`；参考零范数且误差非零时 relative 为 null 且比较失败 |

积分标量指标名形如 `volume_p.p@0.3`、`boundary_p.p@0.3`；向量追加分量，如 `volume_U.U[0]@0.3`。频谱和 POD 也产生可引用的标量指标。重复指标名在写产物前拒绝。

出版图使用 Matplotlib Agg 和 Okabe–Ito 蓝色 `#0072B2`，当前为单序列 6×4 英寸图。图内脚注与文件 metadata 记录 case/job/run/caseDict/source，manifest 再记录文件 hash；300dpi PNG 为 1800×1200。已核对的[探针图示例](shots/wp06-postprocess-probe.png)来自制造解测试。

## 4. API

前缀 `/api/v1`，均需 Bearer 和 case 归属。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/cases/{id}/postprocess:run` | 从 caseDict 生成快照并启动作业；body 可省略，`{"run_dir":"C-run/case-1"}` 可覆盖 runDir |
| GET | `/cases/{id}/postprocess/results` | 读取最近一次完整成功结果；`?job_id=N` 读取指定版本 |

返回包括 `job_id`、`result_dir`、`metrics` 和 `manifest`。作业状态/日志仍使用现有 `/jobs/{id}`、增量日志和 SSE 接口。

## 5. 产物与追溯

```text
.vibeflow/jobs/postprocess-{job_id}-config.json   # 提交时 caseDict 快照
D-post/data/latest-postprocess.json              # 最近一次完整成功指针
D-post/data/postprocess-{job_id}/
  run-config.json
  metrics.json
  artifact-manifest.json
  probes.csv                                     # 配置 pointProbes 时
  {lineSamples_id}.csv
  {frequencySpectra_id}.csv
  {modalQuantities_id}.json
  {plots_id}.pdf / {plots_id}.svg / {plots_id}.png
```

`manifest.source` 记录原 caseDict 路径、SHA-256 和提交时 `dict_revision`，另以 `postprocess_sha256` 保存不含 revision 的后处理语义指纹；`config_sha256` 对应执行快照。输入场、网格和产物另记大小与 hash。产物名冲突、`metrics.json`/`artifact-manifest.json` 覆盖、重复指标名均在更新 latest 前失败。

## 6. readiness 的证据边界

- `report_ready`：有提取结果、使用场单位已声明，且所有已配置参考比较通过；否则在 `blockers` 给出原因。
- `can_claim_verification`：至少一个带明确来源的 verification 比较通过。
- `can_claim_validation`：至少一个带明确来源的 validation 比较通过。当前 smoke 只有制造解 verification，因此该值必须为 false。
- `evidence_level`：validation > verification > regression；没有参考时只能作为行为/回归证据。
- 新作业越界、场缺失、时刻缺失、产物冲突或执行失败时，不更新 `latest-postprocess.json`。

`report:compose`、`report:build` 和 `latex:build` 共用 readiness 门禁：核对后处理语义指纹、工程参数、全部求解场/字典/网格、参考文件、结果时刻清单、metrics/readiness 和全部产物哈希；缺失、空/损坏清单、过期或 `report_ready=false` 在创建报告作业前返回 409。工作流状态、errors 与报告记账变化不改变工程指纹；恢复相同实际输入/配置/参考后可以复用证据。未配置后处理的旧报告仍可编译；A–E 推进是另一条门控。

2026-11-02 报告构建续作：HTML/DOCX 先冻结 Markdown 与本地图片，编译到作业私有目录；结束后再次核对源文件和工程/后处理证据，完整产物才逐文件原子替换，最后提交 `E-report/build/report-manifest.json`（job_id、输入/产物 hash、工程证据标识）。HTML 内嵌图片并输出 MathML，阅读器支持 SVG/公式并继续清洗脚本。取消、编译失败、源或工程变化时保留旧报告；旧作业晚完成不覆盖最新产物。LaTeX 冻结主文档目录、复核复制内容，发布前检查源清单与工程证据，变化时保留旧 PDF。

边界：当前快照仅 fs；远程图片须先本地化，外部 CSS/srcset 暂不支持。LaTeX 任意动态/绝对外部依赖未完整冻结。发布是逐文件原子替换加最后清单，仍不是跨文件、跨副本或外部文件系统写入者之间的事务。

## 7. 验证

```bash
cd platform/server
./.venv/Scripts/python.exe -X utf8 postprocess_smoke.py
```

测试新建一单元制造解，不依赖 `cavity-demo`：体积 1、外表面面积 6，p 在 0/0.1/0.2/0.3 为 2/3/2/1，U 恒为 `(1,0,0)`。独立断言探针、线采样、面积分、体平均、2.5 Hz 主频、POD 能量、L2、CSV 重算、失败保留、Windows/UNC 路径拒绝、产物/指标碰撞和参考值边界。
