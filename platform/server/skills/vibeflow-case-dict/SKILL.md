---
name: vibeflow-case-dict
description: 当需要读取、填写、校验或推进 0-caseDict/caseDict（算例唯一参数源）时使用。覆盖 workflowState 状态机（A 几何/B 网格/C 求解/D 后处理/E 报告/V 开发）、分区结构（caseMeta/intent/physicalProblem/geometry/materials/boundaries/mesh/numericalMethod/execution/postProcessing/report/errors）与"澄清需求→写回 caseDict"的交互规范。
---

# caseDict 操作技能

**支持范围**　通过算例 CaseDict 的读取、校验和保存入口维护参数。A–E 确认还需当前阶段的真实成果和门禁核验。

## 核心原则

`0-caseDict/caseDict` 是算例的唯一参数源。任何阶段推进前先把参数写回它；信息不足时用统一模板向用户提问：

```
参数名:
- 用途:
- 说明:
- 建议:
```

## 状态机

`workflowState` 字段取值 A/B/C/D/E/V，对应目录 A-geo/B-mesh/C-run/D-post/E-report/UDF。
每次任务执行后更新它（写回文件）。状态推进必须经用户确认。

## 填写顺序

1. caseMeta（名称/学科/分析类型/执行形态）
2. intent（用户意图/目标输出/验收标准）
3. physicalProblem（问题类别/时间行为/控制方程）
4. geometry（来源/参数化脚本约定）
5. materials / boundariesLoadsInitials（边界条件）
6. mesh / numericalMethod / execution / postProcessing / report

## 解析与校验

- OpenFOAM 字典语法：`key value;`、嵌套 `key { ... }`、`//` 注释
- 写回前用平台的 caseDict validate 端点验证语法
