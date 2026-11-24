---
name: vibeflow-fds
description: FDS（NIST 火灾动力学模拟器）的执行契约：.fds 输入卡组、网格与通风边界、求解与 Smokeview/CSV 产物。当算例后端选 FDS（火灾/烟气/热释放）时使用。（自配环境参考）
---

# FDS 契约（自有重写版）

**支持范围**　自配环境参考。非特权 Linux 已实际使用 FDS 6.11.2 单线程运行官方固体传热基准，并读回六个测点的 CSV 和 Smokeview 场景索引。按发布包配置随附的 `bin/intelmpi/lib`，以 `OMP_NUM_THREADS=1` 和 `I_MPI_FABRICS=shm` 执行 `fds_openmp heat_conduction_a.fds`。用 `examples/fds/verify_slab.py` 核对原始测量数据。

## 输入

- 单文件 `.fds` 卡组（`&HEAD/&MESH/&VENT/&SURF/&REAC/&DEVC/&SLCF…`）。
- 网格：一个或多个 `&MESH IJK=..., XB=...`；域内障碍物 `&OBST`；通风/开口 `&VENT`；火源在 `&SURF HRRPUA=...` + `&REAC`（燃料/产率）。
- 目录放 `C-run/fds/case-N/`。

## 运行

- `fds jobname.fds`（并行 `mpiexec -n N fds -p ...`，版本相关）。日志 stdout + `jobname.out`。
- 关键监控：`&DEVC` 设备量（温度/速度/浓度探头）出 `case_devc.csv`；`&SLCF` 切片出 Smokeview `.sf`；`case_hrr.csv` 热释放率。
- 物理时间别贪长——烟气模拟按秒计费，先短后长。

## 后处理

- `_devc.csv`/`_hrr.csv` → postprocess.py 出图（见 vibeflow-postprocess）。
- Smokeview 场景平台暂不支持；切片量可走 smv→vtk 转换后再进可视化（或先 CSV 路线）。

## 常见坑

- 网格分辨率判据：D*/δx（火源特征直径/网格尺寸）≥10 才可信——网格报告写 `B-mesh/quality_report.md`。
- `&REAC` 燃料与 `HRRPUA` 不匹配会悄悄错火——先对 hrr.csv 的峰值做 sanity check。
- 通风边界 `OPEN` 与 `HVAC` 混用要小心压降。
