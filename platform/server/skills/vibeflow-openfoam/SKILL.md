---
name: vibeflow-openfoam
description: OpenFOAM 求解阶段的执行契约：C-run/case-* 目录编排、字典与 caseDict 对齐、多工况派生、作业提交与残差监控、MPI 并行、UDF D1-D5 编译验证。当推进 B→C 或在 C-run 阶段工作时使用。
---

# OpenFOAM 求解契约（自有重写版）

**支持范围**　OpenFOAM v2406 的 WSL 与 Linux 已实测网格、原生求解、运行取消、续算和成果发布。托管工作流是主入口，注册 MPI 与手动命令分别使用各自的输入。

## 目录与字典

- 求解目录在 `C-run/` 下：单工况用 `C-run/case-1`；多工况按工况特征命名 `C-run/case-<特征>`（如 `case-Re1000`）。
- 每个 case 目录是标准 OpenFOAM 结构：`0/`（初始场，或 `0.orig/`+复制）、`constant/`（物性 polyMesh）、`system/`（controlDict/fvSchemes/fvSolution/decomposeParDict）。
- **caseDict 是唯一参数源**：字典参数必须与 caseDict 的 `numericalMethod`/`execution`/`boundariesLoadsInitials` 段一致；发现漂移以 caseDict 为准拉回对齐。
- `.foam` 桩文件（`<目录名>.foam` 空文件）是可视化/读取器的入口，平台会自动补。

## 提交与监控（本平台）

- 正常算例使用工作流的托管求解。手动提交 WSL 作业时命令形如：
  `source /usr/lib/openfoam/openfoam2406/etc/bashrc && blockMesh` / `checkMesh` / `<solver>`
- 提交后跟踪：作业面板看实时日志；「残差曲线」页签看各场残差趋势。
- 判断收敛不充分、发散、或 Courant 失控时，先停下来改字典/网格，**不要硬跑到底**。
- 结果产出的时刻目录（`0.1/ 0.2/ …`）写回同一 case 目录。

## 多工况

- 通过 `runs:derive` 派生运行目录，核对每个工况字典和 caseDict 的对应载荷记录。重复运行使用版本化输入与发布。
- 每个 case 独立提交作业，互不干扰。

## MPI 并行

- caseDict `numericalMethod.parallel{enabled,subdomains,decomposition}` → `system/decomposeParDict`。
- 托管路径可提交注册 MPI 作业。手动命令的顺序为 `decomposePar` → `mpirun -np N <solver> -parallel` → `reconstructPar`。

## UDF 开发 D1→D5

1. **D1 需求与边界**：任务类型（新求解器/边界条件/functionObject/源项…）、目标、I/O、验收标准写入 `UDF/README.md`；涉及理论必须查证，禁止凭经验写。
2. **D2 设计**：数学模型、关键类、字典接口、构建与验证方法、风险——全部进 README（可评审态）。
3. **D3 实现**：可复用件进 `UDF/src/<lib>/`、求解器进 `UDF/solver/<name>/`，各带 `Make/files`+`Make/options`；**禁止改 OpenFOAM 原生源码**；输出到 `FOAM_USER_LIBBIN/APPBIN`。
4. **D4 编译与最小验证**（WSL 终端/作业）：`wmake libso UDF/src/<lib>` 或 `wmake UDF/solver/<name>`；验证三件套：`<solver> -help` 能跑、`libs("lib<name>.so")` 能加载、最小算例进入合理迭代；编译日志归档 `UDF/logs/`。
5. **D5 集成**：算例 `system/controlDict` 显式 `application <solver>;` + `libs(...)`；回写 caseDict 相关记录与 workflowState。

## 错误处理

求解失败先读日志定位（`FOAM FATAL ERROR` 段），修复后把病因与解法追加到 `0-caseDict/memory.md`，再重提作业。
