---
name: vibeflow-mesh
description: B 阶段网格契约：blockMesh/snappyHexMesh/gmsh 的选用、配置与质量校验。当从几何推进到网格、写网格字典或评估网格质量时使用。
---

# 网格契约（自有重写版）

**支持范围**　blockMesh、Gmsh、snappyHexMesh 和 cfMesh 有实际网格、质量门禁及成果发布证据。选择与算例输入匹配的 mesh.source。

## 选用

- **blockMesh**：规则块体结构网格（矩形/管道/平板腔体首选），手写 `B-mesh/blockMeshDict` 或直接放算例 `system/`。
- **snappyHexMesh**：复杂几何表面贴体（需要 STL 表面 + `snappyHexMeshDict` + `surfaceFeatureExtractDict`），走 WSL 作业。
- **gmsh**：自由剖分/几何来源多样的场合（WSL 里 `gmsh -3 xxx.geo` 出 .msh，再 `gmshToFoam` 转换）。

## 流程

1. 确认 A 阶段产物可用（scene.json/STEP/STL 在 `A-geo/exports/`）。
2. 写网格字典；参数与 caseDict 的 `mesh` 段（topology/elementFamilies/尺寸/边界层/qualityTargets）对齐。
3. 提交作业（WSL 运行时）：`blockMesh` → **`checkMesh` 必跑**。
4. checkMesh 不过或质量不达标（qualityTargets.policy=fail 时阻塞推进）→ 改尺寸/边界层重来；`warn/allowWithReason` 要在 caseDict 里记录理由。

## 质量判读（checkMesh 输出）

- 网格数、非正交角（non-orthogonality，>70 要警惕）、最大偏斜度（skewness，>4 危险）、开口腔体（closed）、体积为负。
- 结论写入 `B-mesh/quality_report.md`，关键数字回写 caseDict `mesh.artifacts`。

## 与上下阶段的衔接

- 网格目录契约：OpenFOAM 求解时网格进 `C-run/case-*/constant/polyMesh`（blockMesh 在 case 目录内直接跑即可）。
- 推进 C 阶段前：网格可生成、可转换、checkMesh 通过——这是门控（用户确认+字段齐备）。
