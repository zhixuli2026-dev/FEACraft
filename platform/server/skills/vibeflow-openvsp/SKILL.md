---
name: vibeflow-openvsp
description: OpenVSP 飞行器参数化几何+气动分析（VSPAERO）的执行契约：.vsp3 模型、Python API、vspaero 求解与结果。当算例走飞行器设计/气动外形时使用。（自配环境参考）
---

# OpenVSP / VSPAERO 契约（自有重写版）

**支持范围**　自配环境参考。Windows 的 OpenVSP 3.57.1、Python 3.11 和 VSPAERO 已通过实际平台作业验证。样例 `wing_polar.py` 构建并重开 `.vsp3`，运行三点迎角扫描，输出 `.polar`、`.adb` 与 CSV。

## 形态

- 几何模型 `.vsp3`（参数化机身/翼面组件树）；Python API `import openvsp`（WSL/容器装了才可用）。
- 目录放 `A-geo/`（外形）或 `C-run/openvsp/case-N/`（气动计算）。
- 脚本式改型：`SetParmValUpdate`/`Update` → `ExportFile`（STEP/STL/IGES）。

## VSPAERO 分析

- `vsp:openvsp> analysis`：`VSPAEROComputeGeometry`（面积/润湿面积）→ `VSPAEROSweep`（迎角扫描，出 `*.polar`/`*.lod`/`*.rpt`）。
- 气动产物是文本表（CL/CD/CM 随 α）→ postprocess.py 出极曲线（见 vibeflow-postprocess）。
- 网格：DegenGeom 三角面元；面板法别追 CFD 精度——它是概念设计段工具。

## 常见坑

- `.vsp3` 是二进制——别手改，脚本化改参数。
- VSPAERO 的 wake 迭代（`WakeNumIter`）对强后掠/大迎角要加。
- 参考面积（Sref/bref/cref）不手填——用 ComputeGeometry 实测值写回，不然系数全错。
