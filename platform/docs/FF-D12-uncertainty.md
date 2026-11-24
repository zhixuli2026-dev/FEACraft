# FF-D12 标量不确定度传播

`caseDict.postProcessing.derivedQuantities.uncertainty` 继续支持 `metric`、`standard`、`coverage` 和 `source`。针对已有标量公式，可以用 `inputs` 为每个公式输入声明单位和标准不确定度。`coverage` 默认 2。省略 `covariance` 时按独立输入传播。显式矩阵需要独立声明协方差来源，并以相同的输入别名列出完整行和列。

```text
p_correlated
{
    metric weighted_pressure.value;
    source "Source of the input standard uncertainties";
    coverage 2;
    inputs
    {
        x { unit "m^2/s^2"; standard 0.01; }
        y { unit "m^2/s^2"; standard 0.02; }
    }
    covariance
    {
        source "Source of the declared covariance estimates";
        rows
        {
            x { x 0.0001; y 0.0001; }
            y { x 0.0001; y 0.0004; }
        }
    }
}
```

矩阵条目的单位为同行和同列输入单位的乘积。执行器检查行列名、对称性、正半定性及对角与输入标准不确定度平方的一致性。结果记录各输入的局部解析灵敏度、矩阵及来源、合成标准不确定度和扩展不确定度，输出单位沿用公式单位。无法求得可靠的局部灵敏度时会在 `readiness` 记录原因。[执行器](../server/app/services/postprocess_executor.py)

## 实际验收

2026-11-19，通过公开文件上传、带修订条件的 caseDict 保存和 `/postprocess:run`，让真实 Windows VTK 读取此前 icoFoam 作业留下的十个网格、控制和场文件，逐文件复核 SHA-256。VTK 读取 0.2 s 和 0.5 s 的运动压力体平均值，分别为 `0.02227330501391313` 和 `0.02226855869812202`，单位均为 `m^2/s^2`。对 `x + 2*y`，局部灵敏度为 1 和 2。[原始求解](FF-C05-icofoam-serial-cavity.md) · [专项回执](../../tmp/postprocess-uncertainty-d12-03q_xlaw/uncertainty-propagation.json) · [验收入口](../server/postprocess_uncertainty_acceptance.py)

| 配置 | 合成标准不确定度 | 扩展不确定度 |
| --- | ---: | ---: |
| 旧标量声明 | 0.125 | 0.25 |
| 独立输入 | 0.04123105625617661 | 0.08246211251235322 |
| 显式协方差 | 0.0458257569495584 | 0.0916515138991168 |
| 大常数加实际压力，灵敏度为 1 | 0.01 | 0.02 |

错误单位、非正半定矩阵、不对称矩阵、缺少协方差来源、单变量尖点和联合尖点均进入 `readiness` 的诊断。该作业保留诊断结果，`report_ready` 为 `false`。修正配置后新版本的全部有效不确定度与首次一致，首次发布仍能按 ID 读回。三个本地执行范围均已核验停止，隔离 API 端口已关闭，专项退出码为 0，模型调用为 0。[回执与三个发布 ID](../../tmp/postprocess-uncertainty-d12-03q_xlaw/uncertainty-propagation.json)

适用范围为标量公式的局部一阶传播。实际求解场用于数值和发布回归，验收中的输入标准不确定度及协方差为明示的数值样例。公开观测的采集和对齐另有验收，应用到独立实验验证仍需匹配的试验或观测条件。[公开观测](FF-D12-observations.md)
