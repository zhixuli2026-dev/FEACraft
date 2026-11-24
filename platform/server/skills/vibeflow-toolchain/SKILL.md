---
name: vibeflow-toolchain
description: 平台工具链与环境契约：Windows 宿主 + WSL vibeflow-linux 沙箱的分工、作业运行时选择、路径映射、常见环境问题排查。当命令执行环境出问题或需要选择运行时时使用。
---

# 工具链契约（自有重写版，B/S 形态）

**支持范围**　已实测 Windows、非特权 WSL 和 Linux 的工具探测及作业。以当前部署的 /api/v1/runtime/capabilities 和所选作业环境中的探测为准。

## 双运行时

- **Windows 宿主**（默认）：快速本地命令、文件处理、Python（server venv 里有 numpy/matplotlib/cadquery/vtk）。
- **WSL Ubuntu（vibeflow-linux）**：OpenFOAM 等 Linux 求解器、`wmake` 编译链、bash 脚本。作业提交时 `runtime: "wsl"`；终端面板可切 cmd/WSL。
- WSL 里当前 OpenFOAM v2406 环境要先 `source /usr/lib/openfoam/openfoam2406/etc/bashrc`；从 Windows 直接执行带变量的 Bash 命令时使用 `wsl --exec bash -lc`，避免变量在 source 前被默认 shell 展开。
- 路径映射：Windows `D:\x\y` ↔ WSL `/mnt/d/x/y`（平台自动处理；手写命令时注意）。

## 排查速查

| 症状 | 先看 |
|---|---|
| WSL 命令卡住或无响应 | 查看所属作业状态，按作业取消并核验退出；检查目标发行版 |
| `blockMesh: command not found` | 没 source openfoam bashrc |
| 中文路径进 URL 404 | query 必须 URL 编码（平台 API 调用已处理；手写时注意） |
| 大输出阻塞 | 走作业系统而非终端交互 |
| OpenFOAM 命令缺库 | UDF 的 `libs(...)` 没写进 controlDict（见 vibeflow-openfoam 技能 D5） |

## 作业类型语义

`solve/mesh/pvpython/geometry/convert/shell`——语义归类帮助后续统计与配额；命令本体在 `command` 字段。

## 环境自检

先查看实际能力清单与作业运行时。需要诊断时执行 `python -c "import vtk; print(vtk.vtkVersion.GetVTKVersion())"` 或在目标 WSL 里运行 `command -v blockMesh`。
