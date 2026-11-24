# FF-D12 数组公式

现有 `caseDict.postProcessing.derivedQuantities.formulas` 可读取点探针和线采样的实际数组。输入绑定写明 `source`、`field`，可指定从 0 开始的 `component` 和期望的 `unit`。例如探针与线采样的字段均为运动压力，同一公式可以写 `2 * x + y + mean`，其中 `mean` 可以引用已有标量指标。`unit` 缺失于源字段或与声明不符时，作业报告失败。输出单位沿用公式的显式单位配置。[实现](../server/app/services/postprocess_executor.py)

数组运算要求输入按相同顺序具有同一组时间和坐标。线采样在运算前调整为时间在先，输出指标的 `sampling` 记录实际时刻和三维坐标。数组指标可以被后续公式引用。原有标量 DSL、数组运算与函数均使用白名单 AST。[后处理入口](postprocess.md)

## 实际验收

2026-11-19，通过公开文件上传和带修订条件的 caseDict 写入，使用真实 Windows VTK 运行原有 `/postprocess:run`。输入来自此前完成的 icoFoam C05 求解，十个实际网格、控制和场文件的 SHA-256 均已复核。验收通过正式版本化结果、实际探针和线采样 CSV，独立回算两个时刻和两个坐标的四个结果。首轮数组为下表，单位 `m^2/s^2`。[实际验收](../../tmp/postprocess-arrays-d12-y5nke08w/array-formulas.json) · [输入来源](FF-C05-icofoam-serial-cavity.md) · [验收入口](../server/postprocess_array_formula_acceptance.py)

| 时刻 | (0.025, 0.03, 0.005) | (0.075, 0.03, 0.005) |
| --- | ---: | ---: |
| 0.2 | -0.0200667445062 | 0.2662967472823 |
| 0.5 | -0.0200624110623 | 0.2662857949288 |

旧标量、数组链和错误后的复算都通过。相同长度的不同坐标、逆序时间和声明单位不符分别触发三次真实作业失败。失败作业没有发布可用指标，原发布按版本读回且原字节摘要保持。修正配置后生成新发布，四个数组值逐项一致。外部账号的作业提交和发布读取均返回 404。五个原生范围均已核验停止，隔离 API 端口已关闭，验收退出码为 0，模型调用为 0。[实际日志和指标目录](../../tmp/postprocess-arrays-d12-y5nke08w/array-formulas.json)

本次实际数据用于数值与发布回归。D12 另有相关不确定度传播、前次发布自动引用和公开观测采集的验收。[相关不确定度传播](FF-D12-uncertainty.md) · [前次运行引用](FF-D12-previous-reference.md) · [公开观测](FF-D12-observations.md)
