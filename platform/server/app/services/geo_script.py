"""几何执行器脚本（自有实现，兼容原版契约）——由 venv python 以子进程方式执行：

  python -m app.services.geo_script --case <case_root> [--mode preview|export] [--tolerance 0.1]

契约（对标 vibeflow_geo runner，重写）：
  - 用户脚本 {case}/A-geo/geometry.py，入口 build_geometry(ctx) / export_geometry(ctx, objects)
  - ctx.stage(name, obj, color=..., alpha=...) / ctx.show(...) 登记对象
  - preview：镶嵌写 {case}/A-geo/scene.json（schema vibeflow.cadquery.scene.v1）
  - export：写 {case}/A-geo/exports/{name}.step|.stl + geometry_manifest.json
  - 日志：{case}/A-geo/logs/geometry_executor.log
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import traceback
from pathlib import Path, PureWindowsPath
from types import ModuleType

SERVER_ROOT = Path(__file__).resolve().parents[2]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))
from app.services.casedict import parse_casedict
from app.services.geo_validation import write_validation


DEFAULT_TOLERANCE = 0.1


def preview_tolerance(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError('Geometry tolerance must be a positive finite number') from error
    if not math.isfinite(number) or number <= 0:
        raise ValueError('Geometry tolerance must be a positive finite number')
    return number


class Ctx:
    """用户脚本上下文（stage/show/log/artifact_path 与 caseDict 参数）"""

    def __init__(self, case_root: Path, tolerance: float,
                 case_dict_path: Path | None = None, mode: str = "preview", *, log_sink=None):
        self.case_root = case_root
        self.a_geo_dir = case_root / "A-geo"
        self.case_dict_path = case_dict_path or case_root / "0-caseDict" / "caseDict"
        self.mode = mode
        self.tolerance = tolerance
        self.data = parse_casedict(self.case_dict_path.read_text(encoding="utf-8")) \
            if self.case_dict_path.is_file() else {}
        geometry = self.data.get("geometry", {}) if isinstance(self.data, dict) else {}
        self.params = geometry.get("parameters", {}) if isinstance(geometry, dict) else {}
        self.domain = geometry.get("domain", {}) if isinstance(geometry, dict) else {}
        self.stages: list[dict] = []
        self.messages: list[str] = []
        self.log_sink = log_sink

    def stage(self, name, obj, color="#2563eb", alpha=1.0, **meta):
        self.stages.append({"name": name, "object": obj, "color": color,
                            "alpha": alpha, "meta": meta})
        return obj

    def show(self, name, obj, color="#2563eb", alpha=1.0, **metadata):
        return self.stage(name, obj, color=color, alpha=alpha, **metadata)

    def log(self, message):
        line = str(message)
        self.messages.append(line)
        if self.log_sink is not None:
            self.log_sink(line)

    def artifact_path(self, relative):
        path = Path(relative)
        base = self.case_root if path.parts[:1] == ("A-geo",) else self.a_geo_dir
        p = base / path
        p.parent.mkdir(parents=True, exist_ok=True)
        return p


def _shape_of(obj):
    """Get a CadQuery shape for rendering and default export."""
    import cadquery as cq
    from OCP.TopoDS import TopoDS_Shape

    if callable(getattr(obj, "val", None)):
        obj = obj.val()
    if isinstance(obj, cq.Assembly):
        return obj.toCompound()
    if isinstance(obj, TopoDS_Shape):
        return cq.Shape.cast(obj)
    return obj


def _to_hex(color, default="#2563eb"):
    if isinstance(color, str):
        color = color.strip()
        if color.startswith("#") and len(color) in (4, 7) and all(
            digit in "0123456789abcdefABCDEF" for digit in color[1:]
        ):
            return color if len(color) == 7 else "#" + "".join(digit * 2 for digit in color[1:])
    if isinstance(color, (tuple, list)) and len(color) >= 3:
        try:
            channels = [float(channel) for channel in color[:3]]
        except (TypeError, ValueError, OverflowError):
            return default
        if all(math.isfinite(channel) for channel in channels):
            if all(0 <= channel <= 1 for channel in channels):
                channels = [channel * 255 for channel in channels]
            return "#" + "".join(f"{int(max(0, min(255, channel))):02x}" for channel in channels)
    return default


def _make_show_object(ctx: Ctx):
    """全局 show/show_object：对象在前，支持 name/options/color/alpha/opacity/material。"""
    def show_object(obj, name=None, options=None, **kwargs):
        opts = options if isinstance(options, dict) else {}
        label = str(name or kwargs.get("name") or opts.get("name")
                    or f"object_{len(ctx.stages) + 1:02d}")
        color = kwargs.get("color", opts.get("color", "#2563eb"))
        alpha = kwargs.get("alpha", kwargs.get("opacity", opts.get("alpha", opts.get("opacity", 1.0))))
        material = kwargs.get("material", opts.get("material"))
        ctx.stage(label, obj, color=_to_hex(color), alpha=_finite_alpha(alpha), material=material)
        return obj
    return show_object


def _finite_alpha(value):
    try:
        alpha = float(value)
    except (TypeError, ValueError, OverflowError):
        return 1.0
    return alpha if math.isfinite(alpha) else 1.0


def _tessellate(name, obj, color, alpha, tolerance, *, limits=None):
    shape = _shape_of(obj)
    verts, tris = shape.tessellate(tolerance, tolerance)
    if limits is not None and (len(verts) > limits[0] or len(tris) > limits[1]):
        raise ValueError("模型超过 50 万顶点/100 万面预览上限")
    vertices = [[float(v.x), float(v.y), float(v.z)] for v in verts]
    faces = [[int(t[0]), int(t[1]), int(t[2])] for t in tris]
    bb = shape.BoundingBox()
    return {
        "name": name, "color": color, "alpha": alpha,
        "vertices": vertices, "faces": faces,
        "bbox": {"min": [bb.xmin, bb.ymin, bb.zmin], "max": [bb.xmax, bb.ymax, bb.zmax]},
    }


def _ocp_preview(display, raw_objects, tolerance):
    from ocp_tessellate.convert import combined_bb, tessellate_group, to_ocpgroup
    from ocp_tessellate.utils import numpy_to_buffer_json
    from OCP.TopoDS import TopoDS_Shape

    # Assemblies keep their child objects. Convert raw OCCT shapes for the viewer.
    objects = [item['object'] if isinstance(raw_objects[item['name']], TopoDS_Shape)
               else raw_objects[item['name']] for item in display]
    materials = [item["material"] for item in display]
    # ocp-tessellate generates per-face UVs only for material-tagged instances.
    material_options = {"materials": materials} if any(materials) else {}
    group, instances = to_ocpgroup(*objects, names=[item['name'] for item in display],
        colors=[item['color'] for item in display], alphas=[item['alpha'] for item in display],
        **material_options)
    instances, shapes, _ = tessellate_group(group, instances, {
        'deviation': tolerance, 'angular_tolerance': tolerance, 'render_edges': True,
    })
    count, bounds = group.count_shapes(), combined_bb(shapes)
    if not count or bounds is None:
        raise ValueError('OCP preview contains no drawable shapes')
    shapes['bb'] = bounds.to_dict()
    result = {'type': 'data', 'data': numpy_to_buffer_json({'instances': instances, 'shapes': shapes}),
              'count': count, 'config': {'up': 'Z', 'axes': True, 'ortho': True, 'render_edges': True}}
    for _ in json.JSONEncoder(allow_nan=False).iterencode(result):
        pass
    return result


def _resolve_objects(ns: dict, ctx: Ctx) -> dict:
    """build_geometry(ctx) 返回值 → {name: obj}；容错自动探测顶层 CadQuery 对象"""
    result = None
    if callable(ns.get("build_geometry")):
        result = ns["build_geometry"](ctx)
    if result is None and callable(ns.get("build")):
        result = ns["build"](ctx)
    if result is None:
        result = ns.get("result")
        if result is None or (isinstance(result, (dict, list, tuple)) and not result):
            result = ns.get("objects")
    if result is None and ctx.stages:
        # 纯 stage/show 用法：对象集就是登记表
        return {s["name"]: s["object"] for s in ctx.stages}
    if result is None:
        import cadquery as cq
        from OCP.TopoDS import TopoDS_Shape

        cad_types = (cq.Workplane, cq.Shape, cq.Assembly, TopoDS_Shape)
        result = {k: v for k, v in ns.items()
                  if not k.startswith("_") and not isinstance(v, type)
                  and (isinstance(v, cad_types) or callable(getattr(v, "val", None)))}
    if isinstance(result, dict):
        return result
    if isinstance(result, (list, tuple)):
        return {f"object_{i}": o for i, o in enumerate(result)}
    return {"result": result}


def _disable_inline_exports_for_preview(cq, preview: bool):
    if not preview:
        return lambda: None
    original_export = cq.exporters.export
    cq.exporters.export = lambda *_args, **_kwargs: None

    def restore():
        cq.exporters.export = original_export
    return restore


def _export_stems(objects: dict) -> dict[str, str]:
    stems = {}
    for name in objects:
        stem = "".join(c if c.isalnum() or c in "-_" else "_" for c in name) or "object"
        stems[name] = f"{stem}_" if PureWindowsPath(stem).is_reserved() else stem

    original_stems = {stem.casefold() for stem in stems.values()}
    used = set()
    for name, stem in stems.items():
        candidate, suffix = stem, 2
        if candidate.casefold() in used:
            candidate = f"{stem}_{suffix}"
            while candidate.casefold() in original_stems or candidate.casefold() in used:
                suffix += 1
                candidate = f"{stem}_{suffix}"
        stems[name] = candidate
        used.add(candidate.casefold())
    return stems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True)
    ap.add_argument('--execution-id')
    ap.add_argument("--mode", choices=["preview", "export"], default="preview")
    ap.add_argument("--tolerance", type=preview_tolerance, default=DEFAULT_TOLERANCE)
    args = ap.parse_args()

    case_root = Path(args.case).resolve()
    a_geo = case_root / "A-geo"
    (a_geo / "logs").mkdir(parents=True, exist_ok=True)
    log_path = a_geo / "logs" / "geometry_executor.log"
    logf = open(log_path, "w", encoding="utf-8")

    def log(msg):
        logf.write(str(msg) + "\n")
        logf.flush()
        print(msg, flush=True)

    ctx = Ctx(case_root, args.tolerance, case_root / "0-caseDict" / "caseDict", args.mode, log_sink=log)
    script = a_geo / "geometry.py"
    if not script.is_file():
        log(f"geometry script not found: {script}")
        return 2

    try:
        import cadquery as cq  # noqa: F401  校验环境
    except ImportError as e:
        log(f"cadquery 不可用: {e}")
        return 3

    # 以脚本方式执行（注入原版兼容的路径与展示全局名）
    show_object = _make_show_object(ctx)
    module = ModuleType("vibeflow_case_geometry")
    sys.modules[module.__name__] = module
    sys.path.append(str(a_geo))
    sys.dont_write_bytecode = True
    ns = module.__dict__
    ns.update({"ctx": ctx, "__file__": str(script),
                "CASE_ROOT": case_root, "A_GEO_DIR": a_geo, "case_root": case_root,
                "VIBEFLOW_CASE_ROOT": str(case_root),
                "VIBEFLOW_GEOMETRY_SCRIPT": str(script),
                "show_object": show_object, "show": show_object,
                "debug": lambda *values, **_kwargs: ctx.log(" ".join(str(v) for v in values))})
    restore_export = _disable_inline_exports_for_preview(cq, args.mode == "preview")
    try:
        try:
            code = script.read_text(encoding="utf-8")
            exec(compile(code, str(script), "exec"), ns)
            objects = _resolve_objects(ns, ctx)
        finally:
            restore_export()
        log(f"resolved {len(objects)} objects: {list(objects)}")

        # stage() 登记的合并进 objects（stage 优先于返回值）
        for s in ctx.stages:
            objects.setdefault(s["name"], s["object"])

        palette = ["#2563eb", "#16a34a", "#dc2626", "#9333ea", "#0891b2", "#f97316"]
        display = []
        for i, (name, obj) in enumerate(objects.items()):
            sm = next((s for s in ctx.stages if s["name"] == name), None)
            if sm is None:
                sm = next((s for s in ctx.stages if s["object"] is obj), {})
            material = sm.get("meta", {}).get("material")
            material = material.strip() if isinstance(material, str) else None
            display.append({"name": name, "object": _shape_of(obj),
                            "color": _to_hex(sm.get("color", palette[i % len(palette)])),
                            "alpha": _finite_alpha(sm.get("alpha", 1.0)), "material": material})

        validation = write_validation(case_root, display, ctx.tolerance)
        log(f"validation.json written: valid={validation['summary']['valid']} "
            f"watertight={validation['summary']['watertight']} "
            f"manifold={validation['summary']['manifold']}")

        scene_objects = []
        for item in display:
            try:
                scene_objects.append(_tessellate(item["name"], item["object"],
                                                 item["color"], item["alpha"], ctx.tolerance))
            except Exception as exc:
                log(f"skip {item['name']}: {exc}")
        scene = {"schema": "vibeflow.cadquery.scene.v1",
                 "caseRoot": str(case_root), "objects": scene_objects, "messages": ctx.messages,
                 "tolerance": ctx.tolerance}
        try:
            scene['ocp'] = _ocp_preview(display, objects, ctx.tolerance)
            scene['ocp_status'] = {'status': 'ready'}
        except Exception as error:
            reason = 'ocp_unavailable' if isinstance(error, ImportError) else 'ocp_conversion_failed'
            scene['ocp_status'] = {'status': 'fallback', 'reason': reason}
            ctx.log(f'OCP preview fell back to the triangle mesh: {type(error).__name__}: {error}')
        with (a_geo / 'scene.json').open('w', encoding='utf-8') as stream:
            json.dump(scene, stream, ensure_ascii=False, allow_nan=False)
        log(f"scene.json written: {len(scene_objects)} objects")
        if args.mode == "export":
            # export_geometry 仅在显式 export 模式调用；preview 不允许导出副作用
            if callable(ns.get("export_geometry")):
                ns["export_geometry"](ctx, objects)
            exports = a_geo / "exports"
            exports.mkdir(parents=True, exist_ok=True)
            manifest = {"schema": "vibeflow.geometry_manifest.v1",
                        "validation": "../validation.json", "artifacts": []}
            import cadquery as cq
            stems = _export_stems(objects)
            for name, obj in objects.items():
                shape = _shape_of(obj)
                safe = stems[name]
                step = exports / f"{safe}.step"
                stl = exports / f"{safe}.stl"
                cq.exporters.export(shape, str(step))
                cq.exporters.export(shape, str(stl))
                manifest["artifacts"].append({"name": name, "step": step.name, "stl": stl.name})
                log(f"exported {safe}.step/.stl")
            (a_geo / "geometry_manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        import hashlib
        from importlib.metadata import version
        (a_geo / 'toolchain.json').write_text(json.dumps({
            'schema':'vibeflow.geometry-toolchain.v1', 'python':sys.version.split()[0],
            'cadquery':version('cadquery'), 'ocp':version('cadquery-ocp'),
            'worker_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'mode':args.mode, 'execution_id':args.execution_id}), encoding='utf-8')
        log("OK")
        return 0
    except Exception:
        log(traceback.format_exc())
        return 1
    finally:
        logf.close()


if __name__ == "__main__":
    sys.exit(main())
