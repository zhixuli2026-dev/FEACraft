---
name: vibeflow-code-aster
description: Code_Aster 开源结构力学求解器的执行契约：.comm 命令文件、.export 运行描述、run_aster 命令、MED 结果库。当算例后端选 Code_Aster 时使用。
---

# Code_Aster 契约（自有重写版）

**支持范围**　已实测 Windows API 经非特权 WSL 执行串行 run_aster，发布 MED/.mess 并从历史计算库续算。选择 .export 提交 code-aster:solve。

## 输入

- `.comm` 命令文件：按顺序定义 DEBUT/LIRE_MAILLAGE → 模型 AFFE_MODELE → 材料 DEFI_MATERIAU → 载荷 AFFE_CHAR_MECA → 求解 STATIQUE/DYNA_LINE_TRAN → 输出 IMPR_RESU。
- `.export` 运行描述（P 参数声明内存、时间限额，F/R 条目声明输入和输出）。
- 网格 MED 格式（SALOME/Gmsh 转换出）。

## 运行

- 当前验证的 18.1.11 串行构建用 `run_aster xxx.export`。托管提交先封存 .export 声明的输入，在独立 Linux 作业目录运行。
- 产物：`.resu`（文本摘要）、`.rmed`（MED 结果库）、`.mess`（消息日志——**排错只读这里**）。

## 常见坑

- `.mess` 里的 `<A>`（Alarme）不致命、`<F>`（FATAL）致命——读 mess 先搜 `<F>`。
- 收敛失败（非线性）先调 `NEWTON` 的 `RESI_GLOB_MAXI` 与 `PAS_MINI`。
- 材料模型与单元类型（`MODELISATION='3D'`/`'DKT'`…）必须配套。
- MED 后处理进 SALOME/Paraview；平台侧先走 `.resu` 文本解析（postprocess.py）。

## 对接平台

- 提交 `POST /api/v1/cases/{id}/code-aster:solve`，请求体提供 `input_path`。保存计算库时使用 .export 的 base 声明。续算指定原 `publication_id` 与 `checkpoint_path`。
