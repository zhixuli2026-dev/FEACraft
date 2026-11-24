# FF-CB12 异网格参考场

在 `postProcessing.derivedQuantities.referenceFields` 里，`mode cellCenters` 接受单文件 VTU 作为参考体网格。目标场名为 `field`，源场可通过 `referenceField` 指定，省略时使用同名场。`referenceAssociation` 为 `cell` 或 `point`。前者取包含目标单元中心的参考单元值，后者在参考单元内插值。`referenceUnit` 要与目标场的声明单位一致。每个目标单元中心都须有有效参考值。文件、单位、空间覆盖和分量形状一起参与校验，并随同一后处理发布保留来源与文件摘要。 [采样实现](../server/app/services/postprocess_reference_grid.py) · [指标和发布](../server/app/services/postprocess_executor.py)

```text
referenceFields
{
    coarseCell
    {
        field T;
        path D-post/references/coarse.vtu;
        mode cellCenters;
        referenceAssociation cell;
        referenceUnit K;
        kind verification;
        source "OpenFOAM 2406 64-cell native reference at t=1";
        l2_max 0.019;
        time 1;
    }
}
```

已有 `index` 模式继续接受 OpenFOAM 的均匀场或逐单元列表，`mode` 默认 `index`。体积分量按每个单元的体积统一加权，绝对 L2 为 `sqrt(sum(Vᵢ × |targetᵢ − referenceᵢ|²))`。相对 L2 以同样方式除以参考场范数。`postProcessing.objectives.comparisonTargets` 可按指标 `coarseCell.T@1` 和参考 ID `coarseCell` 绑定比较目标。异网格采样与报告沿用同一个就绪门禁。

实际 OpenFOAM `foamToVTK` 将先前的 64 单元导热结果导出为含单元和点数据的 VTU，原件整个写入后处理封存。两种存储分别运行 512 单元导热并确认 E，单元值映射的绝对 L2 为 6.25，相对 L2 为 0.01780047136，与独立解析回算一致。点插值的绝对 L2 为 1.44939049917，两套存储的实际计算一致。两边的比较目标均通过并完成报告组稿。 [独立原生回算](../../tmp/FF-CB12-grid-20261123/native-reference-mapping.json) · [SQLite 文件系统](../../tmp/ff-cb12-reference-grid-cc1g4z_8/cross-grid.json) · [PostgreSQL 认证 SeaweedFS](../../tmp/ff-cb12-reference-grid-t5a06gee/cross-grid.json)

把同一真实参考网格沿 x 平移 2 m 后，512 个单元中心全部落在域外。另用 `degC` 对比目标温标 K，后处理形成带原因的诊断记录。报告接口在准备阶段返回 `input_stale` 并包含两条就绪原因，旧成功指标按版本读回一致。恢复正确参考后，本地生成报告并用实际 Pandoc 编译，HTML 和 DOCX 保留两项比较。 [故障、恢复和正式报告](../../tmp/ff-cb12-reference-report-ui4oc54j/cross-grid-report.json)

认证 SeaweedFS 的报告同样绑定后处理、网格和已应用的生成稿。实际 Pandoc 生成的两个下载文件均包含 `coarseCell.T@1` 和 `coarsePoint.T@1`。另用旧 `index` 模式对 512 单元温度场和均匀 350 K 做对照，绝对 L2 为 28.641098093474，与独立解析值相等。 [认证对象存储报告](../../tmp/ff-cb12-reference-grid-qb5x5qpc/cross-grid.json) · [旧参考场对照](../../tmp/FF-CB12-grid-20261123/legacy-weighted-l2.json)

兼容验证包含实际 400 单元 `icoFoam` 的 `List<vector>` 速度文件，后面带有边界字典。复验修正了内部向量列表的结束位置，实测相对 L2 为 `2.51758066 × 10⁻⁸`，在 VTK 读取精度的 `10⁻⁷` 容差内。 [旧向量场实测](../../tmp/FF-CB12-grid-20261123/legacy-vector-fields.json)

首轮 multipart 验收脚本漏填 API 前缀，随后修复。较早的测试把比较目标放在顶层而导致断言未找到目标，改放 `postProcessing.objectives` 后双存储通过。正式报告复验还发现组合的发布引用顺序与工作区校验顺序不一致，导致本地生成稿编译请求返回 400。修复后按发布 ID 规范顺序封存，并以实际生成稿和后处理结果验证绑定。 [首次报告编译记录](../../tmp/ff-cb12-reference-report-2h1y9on7/cross-grid-report.json)

上面的跨网格用例是数值 verification。另运行现有真实模型入口验证传统后处理读回，DeepSeek `deepseek-flash` 两次 `/v1/chat/completions` 返回 HTTP 200，模拟调用 0。上面的网格与报告作业由真实原生工具执行，模型调用 0。 [真实 DeepSeek 回执](../../tmp/live-cli-startup-94nfy7dv/live-result.json)

## 实测空间剖面

将实测值保存为带 `x,y,z,value` 四列的 UTF-8 CSV，坐标以 m 填写。`postProcessing.derivedQuantities.referenceValues` 用 `metric` 引用具有 `time,point` 轴的采样指标，声明 `kind validation`、实际单位和来源。`profile` 中填写 CSV `path`、`coordinateUnit m` 和求解输出 `time`，可同时附上未经转换的 `sourcePath`。测量坐标须覆盖该计算指标的所有采样点，行序可不同，重复坐标与未匹配的行会产生诊断。

```text
wind_tunnel
{
    metric normalized_xh4.value;
    kind validation;
    unit "1";
    source "Driver and Seegmiller, x/H=4";
    l2_max 0.12;
    profile
    {
        path D-post/references/velocity-xh4.csv;
        sourcePath D-post/references/profiles.exp.dat;
        coordinateUnit m;
        time 2000;
    }
}
```

计算与实测按三个坐标一一配对，输出相对 L2、RMSE、样本数及两个文件的摘要。`comparisonTargets` 的 `type experiment` 可引用此比较。正式报告读取关联发布，显示实测来源、样本数和文件摘要。 [剖面对齐](../server/app/services/postprocess_profile.py) · [工程报告](../server/app/services/report_builder.py)

Driver 和 Seegmiller 风洞的档案保存无量纲流向速度及 `y/H`，对应位置为 `x/H=4`。换算采用台阶高 `H=0.0127 m` 和域厚度中心。独立 OpenFOAM 2406 教程算例共 20,540 个单元，28 个实测样本的相对 L2 为 0.05607。原档与换算表分别计算 SHA-256。该数值只针对所选截面及流向分量。 [风洞档案说明](https://tmbwg.github.io/turbmodels/backstep_val.html) · [OpenFOAM 基准说明](https://doc.openfoam.com/2312/examples/verification-validation/turbulent/backward-facing-step/) · [独立原生回算](../../tmp/FF-CB12-wind-tunnel-20261123/reference.json)

官方教程网格和首轮重分级网格未通过完整 `checkMesh`。托管实算采用同一流道、物性与边界条件，重新分配流向和壁法向单元。26,329 个单元通过生产预设，最大长宽比 14.5889，最小单元行列式 0.010124。SQLite 文件系统的新求解场与同网格独立原生输出的字节一致，28 个样本得到相对 L2 0.0466294 和 RMSE 0.0344736。实际 Pandoc 输出 HTML 与 DOCX 均展示试验来源、样本及原件摘要。偏移一个测量点或把相对 L2 容差改为 0.001，组稿都收到 409，原发布仍可按 ID 查询。 [完整网格检查](../../tmp/FF-CB12-wind-tunnel-20261123/managed-native/log.checkMesh) · [重分级原生回算](../../tmp/FF-CB12-wind-tunnel-20261123/managed-native-reference.json) · [文件系统及负例](../../tmp/ff-cb12-wind-tunnel-idr_4s28/wind-tunnel.json)

可通过 `POST /api/v1/cases/{case_id}/postprocess/observations/wind-tunnel:import` 直接采集上述档案。请求体为 `{"section":4}`，可选截面为 −4、1、4、6、10。响应返回归档 `sourcePath`、归一化坐标 `path`、`receipt`、单位、两个文件摘要和公开来源。将三个路径一起用在 `profile`，封存时核对采集回执与原件，报告增加采集回执摘要。同一来源、截面和原始字节重复采集复用三个文件。 [采集实现](../server/app/services/postprocess_observations.py) · [公开端点](../server/app/routers/postprocess.py)

文件系统通过冷启动 API 实际采集原档，记录 HTTP 200，校验重复采集、无权限账号、无效截面、篡改 JSON 回执与恢复。历史指标按所选发布保持。真实 DeepSeek `deepseek-flash` 从已发布稿件读取原档 SHA-256，工具全文与模型答复均准确，最终验收两次 HTTP 200，模拟 0。首轮验收误用了流式预览的上限，持久工具全文和模型答复已包含正确摘要，改用正式历史 API 核对后通过。 [实际采集与回执拒绝](../../tmp/ff-cb12-wind-provider-rdez_xk2/wind-provider.json) · [真实模型回执](../../tmp/live-wind-tunnel-ihfbjjbm/live-result.json) · [首轮接收记录](../../tmp/live-wind-tunnel-beuf0b6a/live-result.json)

PostgreSQL 和认证 SeaweedFS 也通过实际求解、正式采集与重复采集、回执封存、VTK 同坐标比较、自动组稿及实际 Pandoc。两套存储的压力流动算例使用同一个原生速度场摘要，数值完全一致。下载的报告含实际原档摘要。首次对象存储的组稿等待返回 `composition_pending` 和作业编号，验收脚本起初将它当作终态。复验继续等待相同作业完成，HTML 和 DOCX 均通过；隔离数据库已清除。 [认证对象存储回执](../../tmp/ff-cb12-wind-tunnel-tvm9famo/wind-tunnel.json) · [首轮等待记录](../../tmp/ff-cb12-wind-tunnel-r0tz_n_j/wind-tunnel.json) · [数据库清理](../../tmp/terminal-agent-postgres-zi0b1501/cleanup.json)

2026-11-23，FF-CB12 根据 T02 完成。证据覆盖前次发布引用、实验与公开观测采集、异网格、数组公式、相关不确定度、多序列图和全章节报告。FF-C14B 的其余原黄金算例继续按计划验收。 [整项证据](../../tmp/FF-CB12-wind-tunnel-20261123/validation.json)
