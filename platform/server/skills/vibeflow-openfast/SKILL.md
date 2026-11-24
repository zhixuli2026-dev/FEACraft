---
name: vibeflow-openfast
description: OpenFAST 风机气动弹性仿真（NREL）的执行契约：.fst 主输入+模块子输入（AeroDyn/ElastoDyn/…）、命令与 .out 时序产物。当算例走风机/风电载荷时使用。（自配环境参考）
---

# OpenFAST 契约（自有重写版）

**支持范围**　自配环境参考。Windows 已实际运行 OpenFAST 4.2.1 的官方 AOC_WSt 风机算例至 35 秒，核对 27 个输出频道与参考数据。相应运行环境与原生输入模块版本需匹配。

## 输入

- 主文件 `.fst`（仿真时间/模块开关/模块文件指针）+ 每模块自己的输入（AeroDyn/ElastoDyn/HydroDyn/ServoDyn…）。
- 叶片/塔架/平台几何与属性在 ElastoDyn/AeroDyn 输入里；风廓线由 InflowWind 定义。
- 目录放 `C-run/openfast/case-N/`。

## 运行

- `openfast main.fst`（单机版）；产物 `*.out`（时序表）+ `*.sum`（摘要）+ 可选 VTK 动画帧。
- 气动弹性=多模块耦合：改一个模块参数注意联动（叶片刚度→固有频率→时间步）。
- 风况组合=复制 `.fst`+换 InflowWind 文件——平台的 `runs:derive` 派生正合用。

## 常见坑

- 时间步稳定性：`DT` 必须小于最小固有周期/量级；报数值不稳先砍 DT。
- 模块开关互斥逻辑（CompAero/CompInflow 组合）别乱配。
- `.out` 列名长且多——后处理先挑通道（`Channel` 行）再画。
- 结果回写 caseDict `postProcessing.artifacts`；载荷包络/极值进 `D-post/summary.md`。
