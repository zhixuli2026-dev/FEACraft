---
name: vibeflow-aircraft-design
description: 飞行器概念设计工作流契约（几何 openvsp + 气动 vspaero + 参数扫描编排的串联）。当用户做飞行器概念/初步设计时使用。（自配环境参考）
---

# 飞行器概念设计工作流（自有）

**支持范围**　自配环境参考。Windows 已实际验证 OpenVSP 的无后掠矩形翼建模与 VSPAERO 三点参数扫描，可生成参数化几何、气动结果和升力 CSV。其他设计约束使用对应的几何与分析算例校验。

## 定位

这是**编排技能**：把几何（vibeflow-openvsp）+ 气动分析（VSPAERO/或 OpenFOAM 精算）+ 多工况派生串成设计回路。概念设计段用面元法快筛，详细段再上 CFD。

## 流程

1. **几何**：openvsp 脚本化参数模型（翼展/弦长/后掠/扭转…），导出 STEP/STL 到 `A-geo/exports/`。
2. **快筛**：VSPAERO 迎角/构型扫描——参数组合用 `runs:derive` 派生成工况矩阵，批量跑。
3. **精算（可选）**：筛出的候选外形走 OpenFOAM 全 CFD（vibeflow-openfoam 契约）。
4. **对比**：postprocess.py 汇总极曲线/载荷对比表进 `D-post/summary.md`；报告走 vibeflow-report。

## 常见坑

- 面元法对厚翼/大迎角失真——筛选阶段标注置信度。
- 构型参数改动后参考面积变——气动系数必须重新归一化（用 ComputeGeometry 实测值）。
- 几何与气动网格的命名/坐标系对齐（X 前向/Y 右向/Z 上向的约定先定死）。
