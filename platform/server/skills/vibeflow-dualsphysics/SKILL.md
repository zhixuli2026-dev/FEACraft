---
name: vibeflow-dualsphysics
description: DualSPHysics 光滑粒子流体（SPH）求解器的执行契约：GenCase 几何定义、XML 算例、求解器命令、bi4→VTK 产物链。当算例后端选 DualSPHysics（自由表面流/波浪/晃荡）时使用。（自配环境参考）
---

# DualSPHysics 契约（自有重写版）

**支持范围**　自配 DualSPHysics。非特权 WSL 已实测 5.4.355 CPU 的二维溃坝、粒子检查点和 PartVTK 回读。运行前核对同系列的 GenCase、求解器和 PartVTK。

## 输入链

- 几何/粒子布局由 **GenCase** 从 XML 定义生成，例如 `GenCase_linux64 Case_Def out/Case -save:all`，保留生成的 `out/Case.xml`、`out/Case.bi4` 和几何 VTK。
- 算例 XML 分两段：`<casedef>`（几何/边界/粒子间距 dp）+ `<execution>`（求解参数/时间/输出）。
- dp 是 SPH 的"网格尺寸"——粒子数 ~ 体积/dp³，三维别贪小（百万级起步）。

## 运行（WSL/容器装了才可用）

- 5.4 CPU 版使用 `DualSPHysics5.4CPU_linux64 -cpu -ompthreads:2 out/Case out`。检查 `out/Run.out`、`out/RunPARTs.csv` 和 `out/data/Part_*.bi4`。
- 原输入和完整检查点可用于续算，例如 `DualSPHysics5.4CPU_linux64 -cpu out/Case resumed -partbegin:51:51 out/data`。不同阶段保存独立输出目录，记录实际续算物理时间。

## 后处理

- `PartVTK_linux64 -dirdata out/data -savevtk out/PartFluid -onlytype:-all,fluid -vars:+idp,+vel,+rhop,+press` 导出流体 VTK，保留粒子 ID、速度、密度和压力。可用 `-first:N -last:M` 选择可读帧。
- 自由表面/波高探针：`measuretool -points` 出时序 CSV → postprocess.py 出图。

## 常见坑

- dp 与域尺寸数量级失衡 → 粒子爆炸或时间过长；先粗 dp 验证运动形态再加密。
- XML 里 `<rhop0>` 与运动粘度 `<viscosity>` 参数配套错会数值爆炸。
- 核对算例 XML 选择的边界离散、周期设置和粒子排除条件，结合实际版本日志检查行为。
