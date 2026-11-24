# FF-D23 出版级图表

在 `caseDict` 的 `postProcessing.artifacts.plots` 下添加图表。原有顶层 `source` 和 `field` 继续生成单序列图。多序列通过 `series` 指定，使用 `overlay` 叠加或 `panels` 分面。叠加要求横纵轴单位分别一致，分面可以在同一张图上分别显示时间序列和空间剖面。分面默认一列，`columns 2` 可设两列。每图最多六条序列，分面最多四行。按序列 ID 排列，颜色、线型和标记共同区分曲线。

以下配置使用已定义的 `thermalProbes`，读取 `T` 的两个探针位置。可选误差带使用与纵轴相同的单位，须注明来源。`halfWidth` 可以是统一半宽，也可以按采样顺序给出逐点数组。图上的色带和 CSV 中的上下界都按 `y ± halfWidth` 计算。

```text
thermal_history
{
    layout overlay;
    title "Temperature at two probe positions";
    yLabel Temperature;
    formats (pdf svg png);
    dpi 600;
    series
    {
        cold
        {
            source thermalProbes;
            field T;
            probe 0;
            label "Cold-side Point";
            band { unit K; source "Declared instrument tolerance"; halfWidth (0.25 0.5); }
        }
        hot { source thermalProbes; field T; probe 1; label "Hot-side Point"; }
    }
}
```

探针的横轴为秒，`probe` 从 0 开始。线采样使用距离米，可用 `time` 选取已提取的时刻。向量场使用 `component` 选择分量。无法按同一轴解释的单位、缺失的场、错误分量和不合法的带宽均使作业失败，已发布历史仍可访问。

后处理作业按发布版本保存 PDF、SVG、300–600 dpi PNG 及多序列 `.plot.csv`。图脚注记录算例、作业、运行目录、caseDict 摘要和数据文件。CSV 记录源操作、场、分量、探针或时刻、实际横纵坐标和单位，以及可选的误差带上下界和来源。报告组稿自动插入已发布的 PNG 或 SVG，结果章节记录图件和 CSV 摘要，并可由 Pandoc 构建为 HTML 和 DOCX。

在 `GET /api/v1/cases/{case_id}/postprocess/results` 获取 `result_dir`、图件文件名和发布 ID。将完整产物相对路径作为 `path`，向 `GET /api/v1/cases/{case_id}/postprocess/artifact` 下载。可传 `publication_id` 获取指定历史，或省略它读取当前发布。下载验证所选归档成员的摘要，支持 HEAD、Range 和 `X-Vibeflow-File-Version`。

## 验收

真实非特权 WSL OpenFOAM 运行 `blockMesh` 与 `laplacianFoam`，后处理使用 VTK 和 Matplotlib Agg。热传导稳态引用独立解析式 `T(x) = 300 + 100x K`，初始场为 300 K。叠加和分面共 13 行 CSV 对照解析值和误差带边界，实测平均温度 349.999999944 K。此用例为解析 verification，实验 validation 保持未满足。

公开 API 检查图例、单位、误差带、PDF/SVG 及 300/600 dpi PNG，核对归档成员与清单摘要。验证跨账号拒绝、历史及条件范围下载、异单位叠加失败、实际取消和 API 冷启动重跑。报告通过实际 Pandoc 生成，HTML 和 DOCX 中的插图字节与后处理原件一致。旧单序列指标 JSON 和三种图件另与改动前保存的字节逐一复核。模型调用为 0。[实测汇总](../../tmp/FF-D23-publication-charts-20261122/validation.json)

首轮图像检查发现分面横轴标签与来源脚注重叠，修复后增加了 SVG 坐标断言，并复核了四行单列和六图双列的布局。早期 SeaweedFS 回合在作业列表显示取消后立即终止 API，持久尝试仍为 `cancelling`，随后提交遇到 `recovery_required`。复测等待持久终态与实际停止凭据后再关闭 API。[早期 SeaweedFS 回执](../../tmp/publication-charts-s3-pj5cyfaj/chart-result.json)

最终验收使用 SQLite 搭配文件系统及认证 SeaweedFS。两次取消均保存空执行范围的停止凭据，冷启动后分别读取新发布和历史发布。PNG 的密度元数据分别为 600 和 300 dpi。修改归档 CSV 的一个字节后，HEAD 与范围下载均拒绝，恢复原字节后重试通过。[文件系统](../../tmp/publication-charts-fs-gajsietr/chart-result.json) · [认证 SeaweedFS](../../tmp/publication-charts-s3-ov_zpsc3/chart-result.json) · [分面样图](../../tmp/publication-charts-fs-gajsietr/thermal_panels.png) · [叠加图 CSV](../../tmp/publication-charts-fs-gajsietr/thermal_history.plot.csv)
