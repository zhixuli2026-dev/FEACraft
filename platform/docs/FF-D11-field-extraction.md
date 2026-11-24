# FF-D11 边界积分、法向通量与全场模态

在算例的 `caseDict.postProcessing.dataExtraction` 中配置操作，然后从作业启动入口运行后处理。作业详情的「后处理证据」显示边界名称、积分方法、带符号的指标值和单位。全场模态提供绑定到所选发布版本的 VTU 下载。用 VibeFlow 结果查看器或 ParaView 打开 VTU，在单元数据中选择 `PODMean` 或 `PODMode_1` 等模态。

```text
surfaceIntegrals
{
    outletTemperature { field T; patch xMax; time 1; }
    outletHeatFlux { field flux; patch xMax; mode normalFlux; time 1; }
}
modalQuantities
{
    thermalModes { sourceType cellField; field T; times (0 0.25 0.5 0.75 1); rank 2; }
    fluxModes { sourceType cellField; field flux; times (0.25 0.5 0.75 1); rank 1; }
}
```

## 数值和来源

- `patch` 采用 OpenFOAM 边界的准确名称。`areaIntegrate` 用边界场值逐分量乘面面积求和。省略 `patch` 时以内部网格的整个外表面和邻接单元值积分。空边界、未知边界或缺少场值时作业报告错误。[VTK 的 OpenFOAM 边界选择](https://vtk.org/doc/nightly/html/classvtkOpenFOAMReader.html)
- `normalFlux` 用三分量边界向量与各面有向面积向量的点积求和。正值表示流出，负值表示流入。单位沿用声明的场单位乘平方米。积分依据 OpenFOAM 面的原始点序计算方向。[OpenFOAM 的面积和法向积分](https://doc.openfoam.com/2212/tools/post-processing/function-objects/field/surfaceFieldValue/)
- `pressureForce` 需要指定 `patch` 和标量压力场。`Pa` 可直接积分，`m^2/s^2` 需要 `referenceDensity`（kg/m³）。`referencePressure` 以 Pa 给出，默认 0。每个面使用压差乘有向面积向量，输出 X、Y、Z 三分量力，单位为 N。可用公式合并多个壁面。 [OpenFOAM 压力力项](https://doc.openfoam.com/2306/tools/post-processing/function-objects/forces/forces/)
- `sourceType cellField` 在同一内部网格上读取所有时刻的选定场。默认纳入所有分量，`component` 可选择单一分量。去均值后以单元体积平方根加权，再对时间快照的 Gram 矩阵分解。输出包含奇异值、能量比例、归一化时间模态以及 VTU 的均值与空间模态；均值加上奇异值、时间与空间模态乘积可重建已选阶数的近似场。`detrend false` 时分解原始快照，重建无需加均值。
- 全场 POD 接受 2 至 256 个递增时刻，累计最多 800 万个快照分量。每一步核对网格点、单元类型与连接关系；网格变化、缺场、分量不一致或秩超过数值可用阶数会返回具体错误。探针 POD 仍可用 `source` 指定探针 ID。

## 实测

2026-11-22 在非特权 WSL 运行 OpenFOAM 2406 的 `blockMesh` 和 `laplacianFoam`。非均匀 80 单元导热算例生成 0 至 1 秒五个时刻。直接从 OpenFOAM 文件读取参考值，用显式离散傅里叶和 NumPy SVD 独立回算，VTU 的体积加权空间模态通过正交和截断重建误差检查。文件系统验收核对六类提取、旧的整表面积分和探针 POD、真实取消、API 冷启动及历史 VTU 摘要。数值算例使用解析与数值检查，未建立实验 Validation。[OpenFOAM 日志](../../tmp/FF-D11-field-extraction-20261122/openfoam.log) · [文件系统回执](../../tmp/d11-extraction-fs-64m_3ybo/d11-result.json) · [操作拒绝回执](../../tmp/FF-D11-field-extraction-20261122/operator-negative-result.json)

认证 SeaweedFS 通过相同流程。入口和出口热通量积分实测分别为 `-3.0152228646329604e-05` 和 `6.802999496459961 K·m³/s`。温度场前两阶模态覆盖约 99.9895% 的波动能量。双存储的 VTU 摘要逐一一致，跨账户读取返回 404。Edge 实测 960、650、390 像素宽度，各历史及当前下载核对同一摘要。模型调用为 0。[SeaweedFS 回执](../../tmp/d11-extraction-s3-p92nnzj8/d11-result.json) · [Edge 回执](../../tmp/FF-D11-field-extraction-20261122/browser-result.json) · [模态下载界面](../../tmp/FF-D11-field-extraction-20261122/edge-modal-downloads.png)

旧版图件、CSV、诊断和历史状态通过 Edge 回归。[回归回执](../../tmp/FF-D11-field-extraction-20261122/d24-compatibility/browser-result.json) · [验收汇总](../../tmp/FF-D11-field-extraction-20261122/validation.json)

回退时先将新增的 `patch`、`normalFlux` 和 `sourceType cellField` 作业配置迁回旧算子，再撤回新增算子及 VTU 下载入口。
