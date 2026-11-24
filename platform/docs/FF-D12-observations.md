# FF-D12 公开观测数据采集与对齐

有算例写权限的用户可以通过 `POST /api/v1/cases/{case_id}/postprocess/observations:import` 采集 NOAA/NOS CO-OPS 小时数据。接口支持校验后的小时实测水位和小时潮位预测。请求示例为西雅图站的一天，单位为米，时间使用 UTC，垂直基准为 MLLW。日期范围最多 31 天，CSV 最大 2 MiB。[NOAA 数据接口](https://api.tidesandcurrents.noaa.gov/api/prod/) · [数据使用说明](https://www.tidesandcurrents.noaa.gov/disclaimers.html)

```json
{
  "station": "9447130",
  "begin": "2025-01-15",
  "end": "2025-01-15",
  "product": "hourly_height",
  "datum": "MLLW",
  "units": "metric"
}
```

响应的 `path` 指向 `D-post/observations/` 下的原始 CSV，`receipt` 指向配套回执。回执保存原请求 URL、来源与许可、文件 SHA-256、时间范围、单位、垂直基准、质量标记和实际响应状态。文件和回执随同一算例事务提交。相同结果再次采集时复用既有版本，已修改的原件或回执返回 409。网络读取完成后会重新验证登录与算例写权限。[采集路由](../server/app/routers/postprocess.py) · [NOAA 适配](../server/app/services/postprocess_observations.py)

在 `postProcessing.derivedQuantities.referenceValues` 的 `series` 子块中引用原始 CSV 和回执。下面 `OBSERVATION_CSV` 和 `OBSERVATION_RECEIPT` 为接口返回值占位符。`candidateSeries` 可选择同算例的 CSV 作数据对齐回归，`timeZone` 使用 `UTC` 或 `offset`。后一种要求 CSV 每行时刻显式包含 UTC 偏移。通用 CSV 使用 `timeColumn`、`valueColumn`、`unit`，有垂直基准时声明 `datum`。省略 `candidateSeries` 时，可以引用已有的时间采样指标，以 `timeOrigin` 指明工程时间对应的绝对起点，在多点采样时用 `point` 选择一个真实采样点。[当前 CSV 解析和对齐实现](../server/app/services/postprocess_series.py)

```text
coops_seattle
{
    metric station_series;
    kind regression;
    source "NOAA/NOS CO-OPS Seattle 9447130";
    unit m;
    l2_max 0.11;
    series
    {
        path OBSERVATION_CSV;
        receipt OBSERVATION_RECEIPT;
    }
    candidateSeries
    {
        path D-post/references/noaa-prediction.csv;
        timeColumn "Date Time";
        valueColumn Prediction;
        timeZone UTC;
        unit ft;
        datum MLLW;
    }
}
```

执行器要求两个序列具有相同的唯一 UTC 时刻与垂直基准。单位相同时直接计算，英尺和米可相互换算。结果保存有效样本数、时段、来源路径和摘要以及误差指标。CSV 和回执作为封存输入登记到报告门禁，格式、来源、时刻、单位和基准错误会列入报告就绪诊断。两个外部序列的对齐作为回归证据，`can_claim_validation` 保持为 `false`。[结果与报告门禁](postprocess.md)

## 实际验收

2026-11-19，正式算例 API 从 NOAA 获取西雅图站 `9447130` 的原始实测 CSV（米，826 字节）和官方预测 CSV（英尺，583 字节）。两次实际请求返回 200，各有 24 条记录，覆盖 `2025-01-15 00:00–23:00 UTC`、垂直基准 MLLW，实测的 `I` 和 `L` 均为零。原件摘要与独立直连下载一致。[独立下载记录](../../tmp/ff-d12-observations-20261119/official-downloads.json) · [正式专项回执](../../tmp/postprocess-observations-d12-e9rqws3y/official-observations.json)

封存后的 NOAA 比较和通过普通文件入口上传的通用 CSV 比较都对齐 24 个时刻，预测从英尺换算为米后，RMSE 为 `0.2283845185128508 m`，相对 L2 为 `0.09839307732345533`。已核输入清单包含两份 NOAA 原文件及回执。另用真实 Windows VTK 单独核对 C05 压力采样档案的两个时刻，绝对差为零。NOAA 水位和 C05 压力各自独立比较，两者验收均为回归证据。[验收入口](../server/postprocess_observations_acceptance.py) · [C05 样本来源](FF-C05-icofoam-serial-cavity.md)

其它账号的采集与读取均返回 404，非法请求返回 422，源站空结果返回 502。错时刻、错单位、错基准和缺来源分别生成诊断，该版本的报告构建返回 409。改动实测文件后，重采集和报告均返回 409，历史发布仍可按 ID 读取，恢复原字节后成功发布相同指标。四次本地执行范围已核验停止，隔离 API 端口已关闭，专项退出码为 0，模型调用为 0。[完整终态](../../tmp/postprocess-observations-d12-e9rqws3y/official-observations.json)
