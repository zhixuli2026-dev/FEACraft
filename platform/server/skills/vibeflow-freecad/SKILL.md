---
name: vibeflow-freecad
description: FreeCAD 参数化 CAD 内核的执行契约：无头 Python 宏、文档对象模型（Part/Sketcher/Mesh）、STEP/STL 导出。当几何来源走 FreeCAD 路线（替代/补充 CadQuery）时使用。（自配环境参考）
---

# FreeCAD 契约（自有重写版）

**支持范围**　自配环境参考。Windows 便携版 FreeCAD 1.1.4 的无头控制台已验证四孔板布尔、FCStd 保存重开及 STEP/STL 导出重读。算例为 `examples/freecad/drilled_plate.py`。

## 形态

- 无头批处理：启动 `FreeCADCmd.exe --console`（Linux 用 `freecadcmd`），从其 Python 控制台运行脚本。便携验证用 `runpy.run_path(脚本路径, run_name="__main__")`，并用 `--user-cfg`、`--system-cfg` 指向独立目录。
- 放 `A-geo/`（几何来源脚本）或 `D-post/`（几何后处理）。
- 与平台 CadQuery 路线（vibeflow-geometry 技能）二选一或混用：CadQuery 是参数化脚本首选；FreeCAD 强于复杂拓扑/放样/已有 .FCStd 资产。

## 核心对象

- `Part.makeBox/Cylinder/Sphere/…` → `fuse/cut/common` 布尔 → `Shape.exportStep/exportStl`。
- 放样/扫掠：`Part.makeLoft`/`makeSweepSurface`；草图：`Sketcher` + 约束。
- 网格化出 STL：`Mesh.Mesh(shape.tessellate(tol))`。

## 常见坑

- 布尔失败后 `Shape.isValid()` 必查；`shape.fix()` 能救一半。
- tessellate 的线/角公差参数（0.1/0.5 rad 起）决定 STL 精细度。
- 无头下别碰 GUI 模块（`FreeCADGui` 在 freecadcmd 里不可用）。
- 导出产物落 `A-geo/exports/`（STEP/STL+manifest 契约同 CadQuery 路线），scene.json 预览走平台几何面板。
