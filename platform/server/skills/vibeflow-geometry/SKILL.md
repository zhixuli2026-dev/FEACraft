---
name: vibeflow-geometry
description: 当需要创建或修改参数化几何（A-geo/geometry.py）、触发预览/导出、或解读 scene.json/STEP/STL 产物时使用。CadQuery 脚本约定：实现 build_geometry(ctx) 返回 {名称: 对象}，或 ctx.stage(name, obj, color, alpha) 登记；export_geometry(ctx, objects) 可选。
---

# 参数化几何技能

**支持范围**　平台 Python 环境已实测 CadQuery 的 build_geometry(ctx)、浏览器预览、STEP/STL 和版本化导出。

## 脚本契约（A-geo/geometry.py）

```python
import cadquery as cq

def build_geometry(ctx):
    plate = cq.Workplane("XY").box(80, 60, 10)
    return {"平板": plate}   # dict: 显示名 -> CadQuery 对象
```

- `ctx.stage(name, obj, color="#2563eb", alpha=1.0)`：登记显示属性
- `ctx.log(msg)`：写执行日志
- 备选：`ctx.show(...)` 同 stage；顶层变量自动探测兜底

## 预览与导出

- 预览：`POST /cases/{id}/geometry:preview` → 作业完成后 `A-geo/scene.json`（schema vibeflow.cadquery.scene.v1）在几何预览面板渲染
- 导出：`POST .../geometry:export` → `A-geo/exports/*.step/.stl` + `geometry_manifest.json`
- 日志：`A-geo/logs/geometry_executor.log`（排障先看它）

## 排错

- 脚本异常 → 看日志里的 traceback
- 对象没出现 → 确认 build_geometry 返回了对象字典
- 镶嵌太糙 → 调 preview 的 tolerance（默认 0.1）
