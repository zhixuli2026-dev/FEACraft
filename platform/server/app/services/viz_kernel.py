"""可视化 kernel（M2）：trame + vtkOpenFOAMReader 远程渲染进程

每会话一个独立进程：python viz_kernel.py --foam "<算例目录>/<名>.foam" --port N
管线（命令式数据流，时刻/模式切换时重建）：
  reader → block0 网格 → ArrayCalculator(+Umag) → [表面|切面|等值面|流线|矢量箭头] → mapper

桌面版对应物：QVTK 渲染窗 + ParaView 管线（本 kernel 覆盖 Top filter 核心子集）。
浏览器经 trame 自带客户端（iframe 嵌入 React 壳）交互。

踩坑备注（本文件骨架已按 trame 实证收敛，勿随意改）：
- VSelect 等绑定必须元组语法 v_model=("var",) / items=("vars",)，裸值=静态字面量
- VtkRemoteView 需要 render_window 挂 vtkRenderWindowInteractor（push_image 调 EnableRenderOff）
- 初始帧：on_server_ready 里 Render() 后立刻 view_update()（不要先 update 再 Render）
- change 处理器里要 rw.Render() 再 view_update()，只 mapper.Update() 不重渲帧缓冲
- 状态监听用 @state.change(...)；@trame.decorators.change 只在 @TrameApp 类式应用被收集
- VTK 生命周期：SetInputConnection 不持有生产者引用——filter 入 _LIVE 保活，
  局部算法输出要 DeepCopy 后才能带出函数，否则段错误
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
import unicodedata
from uuid import uuid4

MODES = ["表面", "切面", "表面等值线", "等值面", "流线", "矢量箭头"]
AXES = {"X": 0, "Y": 1, "Z": 2}
CAMERA_VIEW_SCHEMA = "vibeflow.viz.camera-view.v1"


def _camera_view_name(value):
    if not isinstance(value, str):
        raise ValueError("请输入书签名称")
    name = unicodedata.normalize("NFC", value.strip())
    if not name or len(name) > 64 or any(unicodedata.category(char) in {"Cc", "Cf"} for char in name):
        raise ValueError("书签名称须为 1–64 个可显示字符")
    return name


def _camera_view_key(name):
    return hashlib.sha256(name.casefold().encode("utf8")).hexdigest()


def _camera_view_document(value):
    from app.services.animation import _vec3

    keys = {"schema", "name", "position", "focal", "up", "parallel", "scale", "angle"}
    if not isinstance(value, dict) or set(value) != keys or value["schema"] != CAMERA_VIEW_SCHEMA:
        raise ValueError("书签格式无效")
    position, focal, up = (_vec3(value[key], key) for key in ("position", "focal", "up"))
    fx, fy, fz = (target - origin for origin, target in zip(position, focal))
    ux, uy, uz = up
    cross = (fy * uz - fz * uy, fz * ux - fx * uz, fx * uy - fy * ux)
    if not any(cross) or not all(math.isfinite(value) for value in cross):
        raise ValueError("相机方向无效")
    scale, angle = value["scale"], value["angle"]
    if (type(value["parallel"]) is not bool or type(scale) not in (int, float) or
            not math.isfinite(scale) or scale <= 0 or type(angle) not in (int, float) or
            not math.isfinite(angle) or not 0 < angle < 180):
        raise ValueError("相机投影无效")
    return {**value, "name": _camera_view_name(value["name"]), "position": position, "focal": focal, "up": up}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--foam", default="", help="OpenFOAM .foam 桩文件绝对路径")
    ap.add_argument("--data-file", default="", help="通用结果文件绝对路径")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--case-root", default="", help="算例根目录（scenes.py 监听用）")
    ap.add_argument("--pipeline-scope", default="")
    ap.add_argument("--session-nonce", default="")
    ap.add_argument("--profile-sha256", default="")
    args = ap.parse_args()
    if bool(args.foam) == bool(args.data_file):
        raise SystemExit("必须且只能指定 --foam 或 --data-file")
    source_path = Path(args.data_file or args.foam)
    if not source_path.is_file():
        raise SystemExit(f"输入文件不存在: {source_path}")
    case_root = (Path(args.case_root) if args.case_root else source_path.parent.parent.parent).resolve()

    import sys
    server_root = Path(__file__).resolve().parents[2]
    if str(server_root) not in sys.path:
        sys.path.insert(0, str(server_root))
    from app.services.colormap import (
        ADVANCED_SCHEMA, COLOR_SPACES, MAX_COLORMAP_BYTES, ColormapError, colormap_json,
        normalize_colormap, paraview_colormap_xml, parse_paraview_colormap, sample_colormap,
    )
    from app.services.animation import (AnimationError, camera_frame_times,
                                        canonical_camera_track_json,
                                        normalize_camera_track, sample_camera_track)
    from app.services.fs_directory import move_file_new
    from app.services.result_readers import ResultBlockUnavailable, dataset_information, load_result_file, result_file_times
    from app.services import viz_filters as filters
    from app.services import viz_memory
    import vtk
    from trame.app import get_server
    from trame.widgets import client as twclient
    from trame.widgets import html as twhtml
    from trame.widgets import vuetify3 as v3
    from trame.widgets import vtk as twvtk
    from trame_vuetify.ui.vuetify3 import SinglePageWithDrawerLayout  # 带 layout.drawer 槽（SinglePageLayout 没有）

    # ---- 读取器与场清单：OpenFOAM 或通用单文件/series ----
    reader = None
    if args.foam:
        reader = vtk.vtkOpenFOAMReader()
        reader.SetFileName(str(source_path))
        reader.CreateCellToPointOn()          # 单元心 → 点数据（内部做）
        reader.UpdateInformation()
        reader.EnableAllCellArrays()
        time_values = reader.GetTimeValues()
        times = [time_values.GetValue(i) for i in range(time_values.GetNumberOfValues())]
    else:
        times = result_file_times(source_path)

    def load_grid(t: float | None, block_key=None):
        """读取时刻并 DeepCopy，避免局部 reader/filter 被 GC 后引用悬空。"""
        blocks = ()
        if reader is not None:
            if t is not None:
                reader.UpdateTimeStep(t)
            else:
                reader.Update()
            out = reader.GetOutput()
            block = out.GetBlock(0) if out.GetNumberOfBlocks() else None
            cell_fields = [reader.GetCellArrayName(i)
                           for i in range(reader.GetNumberOfCellArrays())]
        else:
            result = load_result_file(source_path, t, block=block_key)
            block = result.dataset
            blocks = result.blocks
            cell_fields = [block.GetCellData().GetArrayName(i)
                           for i in range(block.GetCellData().GetNumberOfArrays())]
        if block is None:
            raise RuntimeError("输入没有 internalMesh/可渲染数据")
        vector = block.GetPointData().GetArray("U")
        has_u = vector is not None and vector.GetNumberOfComponents() == 3
        if has_u:
            calc = vtk.vtkArrayCalculator()
            calc.SetInputData(block)
            calc.AddVectorArrayName("U")
            calc.SetFunction("mag(U)")
            calc.SetResultArrayName("Umag")
            calc.SetAttributeTypeToPointData()
            calc.Update()
            output = calc.GetOutput()
        else:
            output = block
        if reader is None and not has_u:
            # The generic reader already returns an independent data set.
            grid = output
        else:
            grid = output.NewInstance()
            grid.DeepCopy(output)
        if has_u:
            grid.GetPointData().SetActiveVectors("U")
        point_fields = [grid.GetPointData().GetArrayName(i)
                        for i in range(grid.GetPointData().GetNumberOfArrays())]
        return grid, list(dict.fromkeys(cell_fields + point_fields)), blocks

    def supported_fields(grid, names):
        fields = [name for name in names if (array := grid.GetPointData().GetArray(name)) is not None
                  and array.GetNumberOfComponents() == 1]
        return fields or ["几何"]

    def available_blocks(blocks, selected=""):
        return [{"title": "全部数据块", "value": ""}, *(
            {"title": block.label, "value": block.key} for block in blocks
        )] if len(blocks) > 1 or (blocks and selected) else []

    grid0, cell_fields, initial_blocks = load_grid(times[-1] if times else None)
    scalar_fields = supported_fields(grid0, cell_fields)

    bounds = grid0.GetBounds()  # xmin xmax ymin ymax zmin zmax
    diag = ((bounds[1] - bounds[0]) ** 2 + (bounds[3] - bounds[2]) ** 2
            + (bounds[5] - bounds[4]) ** 2) ** 0.5 or 1.0

    # ---- 渲染骨架 ----
    mapper = vtk.vtkPolyDataMapper()
    mapper.SetScalarModeToUsePointFieldData()
    mapper.SetScalarVisibility(1)
    actor = vtk.vtkActor()
    actor.SetMapper(mapper)
    actor.GetProperty().SetLineWidth(2)
    renderer = vtk.vtkRenderer()
    renderer.AddActor(actor)
    renderer.SetBackground(0.06, 0.07, 0.09)

    render_window = vtk.vtkRenderWindow()
    render_window.AddRenderer(renderer)
    render_window.SetOffScreenRendering(1)   # 服务端离屏（对应 EGL/headless 诉求）
    render_window.SetSize(1280, 800)
    interactor = vtk.vtkRenderWindowInteractor()
    render_window.SetInteractor(interactor)

    # ---- trame 状态 ----
    server = get_server("vibeflow-viz", client_type="vue3")
    state, ctrl = server.state, server.controller

    _LIVE: list = []  # 保活当帧 filter 链（SetInputConnection 不持有生产者引用，
                      # 局部 filter 被 GC 会段错误——VTK Python 生命周期经典坑）

    state.case_name = source_path.stem
    state.time_items = [f"t={tv:g}" for tv in times]
    state.time_label = state.time_items[-1] if times else ""
    state.result_block_items = available_blocks(initial_blocks)
    state.result_block = ""
    state.result_block_status = ""
    state.field_items = scalar_fields
    state.field = scalar_fields[-1]
    mapper.SetScalarVisibility(0 if state.field == "几何" else 1)
    state.mode_items = MODES
    state.viz_memory_open = False
    state.viz_memory_auto = True
    state.viz_memory_rows = []
    state.viz_memory_status = ""

    def refresh_memory():
        try:
            values = viz_memory.sample()
        except (OSError,ValueError):
            state.viz_memory_status = "系统内存读数不可用，可以重试。"
            return
        state.viz_memory_rows = [{"k": label, "v": f"{values[key]/1024:,.1f} MiB"}
                                 for key,label in (("process_kib","渲染进程常驻"),("peak_kib","进程峰值"),
                                                   ("available_kib","宿主可用"),("total_kib","宿主合计"))]
        state.viz_memory_status = "采样于 " + datetime.now().astimezone().strftime("%H:%M:%S")

    @state.change("viz_memory_open", "viz_memory_auto")
    def _on_memory_controls(**_):
        if state.viz_memory_open:
            refresh_memory()
    state.mode = "表面"
    state.axis_items = list(AXES.keys())
    state.slice_axis = "Z"
    state.slice_pos = 0.5        # 0..1 沿法向 bbox 比例
    state.iso_value = ""
    state.vector_items = filters.field_names(grid0, (3,))
    state.vector_field = "U" if "U" in state.vector_items else ""
    state.filter_error = ""
    state.seed_count = 40        # 流线种子数
    state.glyph_scale = 1.0      # 箭头缩放系数

    def selected_time():
        try:
            idx = state.time_items.index(state.time_label)
        except (ValueError, AttributeError):
            idx = len(times) - 1
        return times[idx] if times else None

    def current_grid():
        time_value = selected_time()
        try:
            grid, names, blocks = load_grid(time_value, state.result_block or None)
        except ResultBlockUnavailable:
            grid, names, blocks = load_grid(time_value)
            state.result_block = ""
            state.result_block_status = "该时刻没有所选数据块，已显示全部数据块"
        else:
            if state.result_block:
                state.result_block_status = ""
        state.result_block_items = available_blocks(blocks, state.result_block)
        state.field_items = supported_fields(grid, names)
        state.vector_items = filters.field_names(grid, (3,))
        if state.field not in state.field_items:
            state.field = state.field_items[-1]
        mapper.SetScalarVisibility(0 if state.field == "几何" else 1)
        return grid

    def field_range(grid, name):
        arr = grid.GetPointData().GetArray(name)
        return arr.GetRange() if arr is not None else (0.0, 1.0)

    def build_source(grid):
        """按显示模式构建 filter 输出端口（filter 全部入 _LIVE 保活）"""
        _LIVE.clear()
        mode = state.mode
        field = state.field
        grid.GetPointData().SetActiveScalars(field)
        rng = field_range(grid, field)

        if mode == "表面":
            gf = vtk.vtkGeometryFilter()
            gf.SetInputData(grid)
            _LIVE.append(gf)
            return gf.GetOutputPort(), rng

        if mode == "切面":
            ax = AXES.get(state.slice_axis, 2)
            lo, hi = bounds[2 * ax], bounds[2 * ax + 1]
            origin = [0.0, 0.0, 0.0]
            origin[ax] = lo + (hi - lo) * float(state.slice_pos)
            normal = [0.0, 0.0, 0.0]
            normal[ax] = 1.0
            plane = vtk.vtkPlane()
            plane.SetOrigin(origin)
            plane.SetNormal(normal)
            cutter = vtk.vtkCutter()
            cutter.SetCutFunction(plane)
            cutter.SetInputData(grid)
            _LIVE.extend([plane, cutter])
            return cutter.GetOutputPort(), rng

        if mode in {"表面等值线", "等值面"}:
            chain, limits = filters.contour(vtk, grid, field, state.iso_value, volume=mode == "等值面",
                                            radius=diag * 0.002)
            _LIVE.extend(chain)
            return chain[-1].GetOutputPort(), limits

        if mode == "流线":
            vector = filters.vector_field(grid, state.vector_field)
            grid, _ = filters.point_field(vtk, grid, vector, (3,))
            # 种子线：域中心横贯线（垂直于流向场主轴 X）
            cx = (bounds[0] + bounds[1]) / 2
            cy = (bounds[2] + bounds[3]) / 2
            cz = (bounds[4] + bounds[5]) / 2
            line = vtk.vtkLineSource()
            line.SetPoint1(cx - (bounds[1] - bounds[0]) * 0.4, cy, cz)
            line.SetPoint2(cx + (bounds[1] - bounds[0]) * 0.4, cy, cz)
            line.SetResolution(max(2, int(state.seed_count)))
            st = vtk.vtkStreamTracer()
            st.SetInputData(grid)
            st.SetInputArrayToProcess(0, 0, 0, vtk.vtkDataObject.FIELD_ASSOCIATION_POINTS, vector)
            st.SetSourceConnection(line.GetOutputPort())
            st.SetIntegratorTypeToRungeKutta2()
            st.SetIntegrationDirectionToBoth()
            st.SetMaximumPropagation(diag * 5)
            st.SetInitialIntegrationStep(diag / 200)
            st.SetMinimumIntegrationStep(diag / 2000)
            st.SetMaximumNumberOfSteps(2000)
            tube = vtk.vtkTubeFilter()   # 线加粗成管，远看可见
            tube.SetInputConnection(st.GetOutputPort())
            tube.SetRadius(diag * 0.003)
            tube.SetNumberOfSides(6)
            _LIVE.extend([line, st, tube])
            return tube.GetOutputPort(), rng

        if mode == "矢量箭头":
            vector = filters.vector_field(grid, state.vector_field)
            grid, _ = filters.point_field(vtk, grid, vector, (3,))
            arrow = vtk.vtkArrowSource()
            mask = vtk.vtkMaskPoints()      # vtk9.6 的 vtkGlyph3D 无 SetOnRatio → 前置抽稀
            mask.SetInputData(grid)
            mask.SetOnRatio(max(1, grid.GetNumberOfPoints() // 200))  # 密度封顶 ~200 个
            mask.RandomModeOff()
            glyph = vtk.vtkGlyph3D()
            glyph.SetInputConnection(mask.GetOutputPort())
            glyph.SetSourceConnection(arrow.GetOutputPort())
            glyph.SetInputArrayToProcess(1, 0, 0, vtk.vtkDataObject.FIELD_ASSOCIATION_POINTS, vector)
            glyph.SetVectorModeToUseVector()      # 用活动向量 U 定向
            glyph.SetScaleModeToScaleByVector()   # 模长定大小
            glyph.SetScaleFactor(diag * 0.08 * float(state.glyph_scale))
            glyph.OrientOn()
            _LIVE.extend([arrow, mask, glyph])
            return glyph.GetOutputPort(), rng

        raise ValueError(f"未知显示模式 {mode}")

    def rebuild(reset_cam: bool = False, *, grid=None):
        if grid is None:
            grid = current_grid()
        previous = list(_LIVE)
        try:
            src, rng = build_source(grid)
        except filters.FilterInputError as error:
            _LIVE[:] = previous
            state.filter_error = str(error)
            return
        state.filter_error = ""
        mapper.SetInputConnection(src)
        mapper.SelectColorArray(state.field)
        if rng[1] - rng[0] < 1e-30:
            rng = (rng[0] - 0.5, rng[1] + 0.5)   # 退化范围（等值线上恒定场值）防 LUT 映射塌缩
        _apply_mapper_colormap(mapper, rng, preview=True)
        _last_rng.clear(); _last_rng.append(rng)
        mapper.Update()
        if reset_cam:
            renderer.ResetCamera()
        renderer.ResetCameraClippingRange()
        render_window.Render()
        ctrl.view_update()
        _sync_info(grid)
        if state.viz_memory_open and state.viz_memory_auto:
            refresh_memory()

    # 状态监听：函数式脚本里必须用 @state.change（@trame.decorators.change 只在
    # @TrameApp 类式应用里被收集——裸函数上等于没注册，换模式不重渲就源于此）
    @state.change("time_label", "field", "result_block")
    def _on_data(**_):
        rebuild(reset_cam=False)

    @state.change("cmap_name", "cmap_invert", "cmap_log", "cmap_min", "cmap_max", "cmap_opacity")
    def _on_cmap(**_):
        _apply_cmap_all()

    @state.change("mode", "slice_axis", "slice_pos", "iso_value", "seed_count", "glyph_scale", "vector_field")
    def _on_structure(**_):
        rebuild(reset_cam=True)

    saved_selection = None
    selection_loaded = False
    selection_names = []
    selection_anchor = ""

    def _save_selection():
        state.vf_saved_selection = {
            "active": state.vf_selected,
            "anchor": selection_anchor,
            "items": [{"name": name, "kind": vf_objects[name]["spec"]["kind"]} for name in selection_names],
        }

    def _set_selection(names, active="", anchor="", *, persist=True):
        nonlocal saved_selection, selection_names, selection_anchor
        selection_names = [name for name in dict.fromkeys(names) if name in vf_objects]
        active = active if active in selection_names else (selection_names[-1] if selection_names else "")
        selection_anchor = anchor if anchor in vf_objects else active
        previous = state.vf_selected
        state.vf_selected = active
        state.vf_selection_count = len(selection_names)
        if active and active != previous:
            _vf_inspect(active)
        if persist:
            saved_selection = None
            _save_selection()
        _sync_vf_state()
        _sync_info()

    def _restore_available_selection():
        if saved_selection is None:
            return
        names = [name for name, kind in saved_selection["items"].items()
                 if name in vf_objects and vf_objects[name]["spec"]["kind"] == kind]
        _set_selection(names, saved_selection["active"], saved_selection["anchor"], persist=False)

    @ctrl.trigger("vf_restore_selection")
    def restore_selection(raw=None):
        import json

        nonlocal saved_selection, selection_loaded
        if selection_loaded:
            return
        selection_loaded = True
        if not isinstance(raw, str) or len(raw) > 64 * 1024:
            return
        try:
            value = json.loads(raw)
        except ValueError:
            return
        if not isinstance(value, dict) or not isinstance(value.get("items"), list):
            return
        items = {item["name"]: item["kind"] for item in value["items"]
                 if isinstance(item, dict) and isinstance(item.get("name"), str)
                 and isinstance(item.get("kind"), str)}
        active, anchor = value.get("active"), value.get("anchor")
        saved_selection = {"items": items, "active": active if isinstance(active, str) else "",
                           "anchor": anchor if isinstance(anchor, str) else ""}
        _restore_available_selection()

    @ctrl.trigger("vf_pick")
    def _vf_select(name, mode="single"):
        if not isinstance(name, str) or name not in vf_objects:
            return
        if mode == "toggle":
            if name in selection_names:
                _set_selection([key for key in selection_names if key != name],
                               state.vf_selected if state.vf_selected != name else "", name)
            else:
                _set_selection([*selection_names, name], name, name)
        elif mode == "range":
            order = [item["name"] for item in state.vf_items]
            if selection_anchor not in order:
                _set_selection([name], name, name)
                return
            start, end = sorted((order.index(selection_anchor), order.index(name)))
            _set_selection(order[start:end + 1], name, selection_anchor)
        elif mode == "context" and name in selection_names:
            _set_selection(selection_names, name, selection_anchor)
        else:
            _set_selection([name], name, name)

    @ctrl.trigger("vf_context")
    def _vf_context(name, x, y):
        if not isinstance(name, str) or name not in vf_objects:
            return
        if type(x) not in (float, int) or type(y) not in (float, int):
            return
        _vf_select(name, "context")
        state.vf_menu_point = [x, y]
        state.vf_menu_open = True

    @ctrl.trigger("vf_action")
    def _vf_action(action, name=None):
        if action == "clear":
            _set_selection([])
            return
        if name is not None and (not isinstance(name, str) or name not in vf_objects):
            return
        targets = [name] if name is not None else selection_names[:]
        if action == "remove":
            for target in targets:
                _remove_vf_actor(target)
            _set_selection(selection_names, state.vf_selected, selection_anchor)
        elif action in ("show", "hide", "toggle", "isolate"):
            affected = vf_objects if action == "isolate" else targets
            for target in affected:
                actor = vf_objects[target].get("actor")
                if actor is None:
                    continue
                if action == "toggle":
                    actor.SetVisibility(not actor.GetVisibility())
                else:
                    actor.SetVisibility(action == "show" or (action == "isolate" and target in targets))
            _sync_vf_state()
        else:
            return
        _vf_render()

    def _vf_inspect(name):
        spec = vf_objects[name]["spec"]
        state.edit_field = spec.get("field") or state.field
        grid = vf_scene["grid"]
        state.edit_scalar_items = filters.field_names(grid, (1,)) if spec["kind"] == "isosurface" else state.field_items
        state.edit_vector_items = filters.field_names(grid, (3,))
        state.edit_mode_items = filters.field_names(grid, (1, 3))
        state.edit_vector = spec.get("vector") or (spec.get("field") if spec.get("field") in state.edit_vector_items else None)
        state.edit_mode_field = spec.get("mode_field")
        state.edit_phase = spec.get("phase", 0)
        state.edit_imaginary = spec.get("imaginary")
        state.edit_axis = spec.get("axis", "Z")
        state.edit_pos = float(spec.get("pos", 0.5))
        iso = spec.get("iso")
        state.edit_iso = ", ".join(map(str, iso)) if isinstance(iso, (list, tuple)) else ("" if iso is None else str(iso))
        state.edit_seeds = int(spec.get("seeds", 40))
        state.edit_scale = float(spec.get("scale", 1.0))

    @state.change("vf_apply_tick")
    def _on_apply(**_):
        _vf_apply_edit()

    def _vf_apply_edit():
        """编辑态写回 spec → 增量重建该对象（Inspector 语义：Apply）"""
        name = state.vf_selected
        ent = vf_objects.get(name)
        if not ent:
            return
        spec = dict(ent["spec"])
        spec["field"] = state.edit_field
        kind = spec["kind"]
        try:
            if kind == "slice":
                spec.update(axis=state.edit_axis, pos=float(state.edit_pos))
            elif kind == "contour":
                spec["iso"] = float(state.edit_iso or 0.0)
            elif kind == "isosurface":
                spec["iso"] = state.edit_iso
            elif kind == "stream":
                spec.update(seeds=int(state.edit_seeds), vector=state.edit_vector)
            elif kind in {"glyph", "deform"}:
                spec.update(scale=filters.finite_number(state.edit_scale, "倍率"), vector=state.edit_vector)
            elif kind == "mode":
                spec.update(scale=filters.finite_number(state.edit_scale, "模态倍率"), mode_field=state.edit_mode_field,
                            phase=filters.finite_number(state.edit_phase, "相位"), imaginary=state.edit_imaginary)
            _create_vf_object(name, spec)
        except ValueError as error:
            state.edit_error = str(error)
            return
        state.edit_error = ""
        _viz_log(f"~{name} inspector 编辑")
        _vf_render()

    # ---------- Animation v1：时间播放 + 帧序列导出 ----------
    _play_task: list = []    # 单槽（当前播放任务）

    @state.change("playing")
    def _on_play(**_):
        if not times:
            state.playing = False
            return
        if state.playing:
            async def _play_loop():
                while state.playing:
                    try:
                        idx = state.time_items.index(state.time_label)
                    except ValueError:
                        idx = -1
                    with state:
                        state.time_label = state.time_items[(idx + 1) % len(times)]
                    await asyncio.sleep(1.0 / max(0.2, float(state.anim_fps or 2)))
            _play_task.clear()
            _play_task.append(asyncio.get_running_loop().create_task(_play_loop()))
        elif _play_task:
            _play_task[0].cancel()
            _play_task.clear()

    def _export_frames():
        """逐时刻渲染出帧 → D-post/animations/frame_t*.png（Animation 导出语义）"""
        if not times:
            return
        out_dir = case_root / "D-post" / "animations"
        out_dir.mkdir(parents=True, exist_ok=True)
        saved = []
        cur = state.time_label
        try:
            for i, tv in enumerate(times):
                state.time_label = state.time_items[i]   # 触发 _on_data 重建
                rebuild(reset_cam=False)
                render_window.Render()
                p = out_dir / f"frame_{i:03d}_t{tv:g}.png"
                w2i = vtk.vtkWindowToImageFilter(); w2i.SetInput(render_window); w2i.Update()
                w = vtk.vtkPNGWriter(); w.SetInputConnection(w2i.GetOutputPort())
                w.SetFileName(str(p)); w.Write()
                saved.append(p.name)
        finally:
            state.time_label = cur
            rebuild(reset_cam=False)
        _viz_log(f"导出帧序列 {len(saved)} 帧 → D-post/animations/")
        state.scenes_status = f"已导出 {len(saved)} 帧动画帧"

    # ---------- 相机关键帧：独立时间轴播放与逐帧导出 ----------
    center = [(bounds[0] + bounds[1]) / 2, (bounds[2] + bounds[3]) / 2,
              (bounds[4] + bounds[5]) / 2]
    DEFAULT_CAMERA_TRACK = {
        "schema": "vibeflow.animation.camera.v1", "fps": 2, "loop": False,
        "frames": [
            {"t": 0, "position": [center[0], center[1], center[2] + diag * 2],
             "focal": center, "up": [0, 1, 0]},
            {"t": 1, "position": [center[0] + diag * 2, center[1], center[2]],
             "focal": center, "up": [0, 1, 0]},
        ],
    }

    def _camera_at(time_value):
        frame = sample_camera_track(state.camera_track_obj, time_value)
        camera = renderer.GetActiveCamera()
        camera.SetPosition(*frame["position"])
        camera.SetFocalPoint(*frame["focal"])
        camera.SetViewUp(*frame["up"])
        state.camera_time = float(time_value)
        renderer.ResetCameraClippingRange()
        render_window.Render()
        ctrl.view_update()

    def _apply_camera_track():
        try:
            track = normalize_camera_track(state.camera_track_text)
            state.camera_track_obj = track
            state.camera_track_text = canonical_camera_track_json(track)
            state.camera_status = f"已应用 {len(track['frames'])} 个关键帧"
            _camera_at(track["frames"][0]["t"])
            return True
        except AnimationError as error:
            state.camera_status = f"轨道错误: {error}"
            return False

    def _export_camera_track():
        saved_view = _camera_snapshot()
        saved_time = state.camera_time
        if not _apply_camera_track():
            return
        track = state.camera_track_obj
        out_dir = case_root / "D-post" / "animations"
        out_dir.mkdir(parents=True, exist_ok=True)
        saved = []
        try:
            for index, time_value in enumerate(camera_frame_times(track)):
                _camera_at(time_value)
                path = out_dir / f"camera_{index:03d}.png"
                w2i = vtk.vtkWindowToImageFilter(); w2i.SetInput(render_window); w2i.Update()
                writer = vtk.vtkPNGWriter(); writer.SetInputConnection(w2i.GetOutputPort())
                writer.SetFileName(str(path)); writer.Write()
                saved.append(path.name)
        finally:
            state.camera_time = saved_time
            _apply_camera_snapshot(saved_view)
        state.camera_status = f"已导出 {len(saved)} 帧相机轨道"

    _camera_task: list = []
    camera_undo = None
    camera_source = source_path.resolve().relative_to(case_root).as_posix()
    camera_folder = Path("D-post", "camera-views", hashlib.sha256(camera_source.encode("utf8")).hexdigest())

    def _camera_path(relative):
        path = case_root / relative
        if path.resolve() != path:
            raise ValueError("视角文件路径已重定向")
        return path

    def _camera_temporary(suffix):
        scratch = _camera_path(Path(".vibeflow", "attempts"))
        scratch.mkdir(parents=True, exist_ok=True)
        return NamedTemporaryFile(dir=scratch, suffix=suffix, delete=False)

    def _camera_snapshot():
        camera = renderer.GetActiveCamera()
        return {"position": list(camera.GetPosition()), "focal": list(camera.GetFocalPoint()),
                "up": list(camera.GetViewUp()), "parallel": bool(camera.GetParallelProjection()),
                "scale": camera.GetParallelScale(), "angle": camera.GetViewAngle()}

    def _camera_render():
        renderer.ResetCameraClippingRange()
        render_window.Render()
        ctrl.view_update()

    def _apply_camera_snapshot(saved):
        camera = renderer.GetActiveCamera()
        camera.SetPosition(*saved["position"])
        camera.SetFocalPoint(*saved["focal"])
        camera.SetViewUp(*saved["up"])
        camera.SetParallelProjection(saved["parallel"])
        camera.SetParallelScale(saved["scale"])
        camera.SetViewAngle(saved["angle"])
        _camera_render()

    def _remember_camera():
        nonlocal camera_undo
        camera_undo = _camera_snapshot()
        state.camera_can_undo = True

    def _undo_camera():
        nonlocal camera_undo
        if camera_undo is not None:
            _apply_camera_snapshot(camera_undo)
            camera_undo = None
            state.camera_can_undo = False
            state.camera_view_status = "已撤回视角操作"

    def _camera_preset(name):
        _remember_camera()
        camera = renderer.GetActiveCamera()
        offset = {"+X": [2 * diag, 0, 0], "+Y": [0, 2 * diag, 0],
                  "+Z": [0, 0, 2 * diag], "iso": [2 * diag, 2 * diag, 2 * diag]}[name]
        camera.SetPosition(*[center[i] + offset[i] for i in range(3)])
        camera.SetFocalPoint(*center)
        camera.SetViewUp(0, 0, 1) if name != "+Z" else camera.SetViewUp(0, 1, 0)
        state.camera_view_status = f"视角 {name}"
        _camera_render()

    def _save_view():
        state.saved_camera_view = _camera_snapshot()
        state.camera_view_status = "已保存当前视角"

    def _restore_view():
        saved = state.saved_camera_view
        if not saved:
            state.camera_view_status = "尚未保存视角"
            return
        _remember_camera()
        _apply_camera_snapshot(saved)
        state.camera_view_status = "已恢复保存的视角"

    def _read_camera_view(path):
        path = _camera_path(path.relative_to(case_root))
        with path.open("rb") as stream:
            data = stream.read(8193)
        if len(data) > 8192:
            raise ValueError("视角书签过大")
        document = _camera_view_document(json.loads(data))
        if path.stem != _camera_view_key(document["name"]):
            raise ValueError("视角书签名称不匹配")
        return document

    def _load_camera_views(notify=False):
        names = []
        invalid = 0
        try:
            folder = _camera_path(camera_folder)
            for path in folder.glob("*.json"):
                try:
                    names.append(_read_camera_view(path)["name"])
                except (OSError, OverflowError, ValueError):
                    invalid += 1
        except (OSError, ValueError):
            state.camera_view_status = "读取视角书签失败"
            return
        state.camera_bookmark_names = sorted(names, key=str.casefold)
        if state.camera_bookmark_selected not in names:
            state.camera_bookmark_selected = state.camera_bookmark_names[0] if names else ""
        if invalid:
            state.camera_view_status = f"有 {invalid} 个视角书签无法读取"
        elif notify:
            state.camera_view_status = f"已读取 {len(names)} 个视角书签"

    @state.change("camera_bookmark_name")
    def _clear_camera_bookmark_error(**_):
        state.camera_bookmark_error = ""

    @ctrl.trigger("camera_save_bookmark")
    def _save_camera_bookmark():
        try:
            name = _camera_view_name(state.camera_bookmark_name)
        except ValueError as error:
            state.camera_bookmark_error = str(error)
            return
        temporary = None
        path = None
        try:
            path = _camera_path(camera_folder / (_camera_view_key(name) + ".json"))
            document = _camera_view_document({"schema": CAMERA_VIEW_SCHEMA, "name": name, **_camera_snapshot()})
            with _camera_temporary(".camera-view.tmp") as stream:
                temporary = Path(stream.name)
                stream.write((json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf8"))
                stream.flush()
                os.fsync(stream.fileno())
            move_file_new(temporary, path, root=case_root)
            _load_camera_views()
            state.camera_bookmark_selected = name
            state.camera_bookmark_error = ""
            state.camera_view_status = f"已保存视角书签 {name}"
        except (OSError, ValueError):
            state.camera_bookmark_error = "同名书签已存在" if path is not None and path.exists() else "保存视角书签失败"
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _apply_camera_bookmark():
        try:
            name = _camera_view_name(state.camera_bookmark_selected)
            path = _camera_path(camera_folder / (_camera_view_key(name) + ".json"))
            document = _read_camera_view(path)
        except (OSError, OverflowError, ValueError):
            state.camera_view_status = "该视角书签无法读取，当前视角已保留"
            return
        _remember_camera()
        _apply_camera_snapshot(document)
        state.camera_view_status = f"已应用视角书签 {document['name']}"

    def _shot_view(retain=False):
        name = "view-preset.png"
        if retain:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            name = f"camera-{stamp}-{uuid4().hex[:12]}.png"
        relative = Path("D-post", "figures", name)
        temporary = None
        try:
            path = _camera_path(relative)
            with _camera_temporary(".png") as stream:
                temporary = Path(stream.name)
            render_window.Render()
            w2i = vtk.vtkWindowToImageFilter()
            w2i.SetInput(render_window)
            w2i.Update()
            writer = vtk.vtkPNGWriter()
            writer.SetInputConnection(w2i.GetOutputPort())
            writer.SetFileName(str(temporary))
            writer.Write()
            with temporary.open("rb+") as stream:
                if writer.GetErrorCode() or stream.read(8) != b"\x89PNG\r\n\x1a\n":
                    raise OSError("PNG 截图写入失败")
                os.fsync(stream.fileno())
            if retain:
                move_file_new(temporary, path, root=case_root)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary.replace(path)
            state.camera_view_status = f"{'已保留截图' if retain else '已截图'} {relative.as_posix()}"
        except (OSError, ValueError):
            state.camera_view_status = "截图保存失败"
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @state.change("camera_playing")
    def _on_camera_play(**_):
        if state.camera_playing:
            async def _camera_loop():
                track = state.camera_track_obj
                start, end = track["frames"][0]["t"], track["frames"][-1]["t"]
                try:
                    with state:
                        if state.camera_time >= end:
                            _camera_at(start)
                        state.camera_status = "相机轨道播放中"
                    while state.camera_playing:
                        await asyncio.sleep(1.0 / track["fps"])
                        with state:
                            next_time = state.camera_time + 1.0 / track["fps"]
                            if next_time > end + 1e-9 and (state.camera_loop or track["loop"]):
                                next_time = start
                            _camera_at(min(next_time, end))
                            if next_time >= end and not (state.camera_loop or track["loop"]):
                                state.camera_playing = False
                                state.camera_status = "相机轨道播放完成"
                except AnimationError as error:
                    with state:
                        state.camera_playing = False
                        state.camera_status = f"轨道错误: {error}"
            _camera_task.clear()
            _camera_task.append(asyncio.get_running_loop().create_task(_camera_loop()))
        elif _camera_task:
            _camera_task[0].cancel()
            _camera_task.clear()

    information_context = {"grid": None, "selection": None, "running": False}

    async def read_paraview_information(**_):
        import json
        import subprocess
        from app.services.paraview import paraview_executable

        selection = information_context["selection"]
        if selection is None or information_context["running"]:
            return
        path, time_value = selection
        information_context["running"] = True
        with state:
            state.paraview_busy = True
            state.paraview_message = "正在读取 ParaView 源数据…"
        try:
            if not path.is_relative_to(case_root.resolve()):
                raise ValueError("所选源文件不在算例中")
            source_before = path.stat()
            args = [paraview_executable("pvpython"), "-m", "app.services.result_readers", str(path)]
            if time_value is not None:
                args.append(f"--time={time_value}")
            flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
            process = await asyncio.create_subprocess_exec(
                *args, cwd=server_root, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **flags,
            )
            try:
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=30)
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            prefix = b"VIBEFLOW_PARAVIEW_INFORMATION "
            records = [line[len(prefix):] for line in stdout.splitlines() if line.startswith(prefix)]
            if process.returncode or len(records) != 1:
                raise ValueError("ParaView 未能读取所选源文件")
            source_after = path.stat()
            if (source_before.st_size, source_before.st_mtime_ns) != (source_after.st_size, source_after.st_mtime_ns):
                raise ValueError("源文件在读取期间改变，请重试")
            measured = json.loads(records[0])
            if information_context["selection"] is selection:
                with state:
                    state.paraview_rows = information_rows(measured, time_value)
                    state.paraview_arrays = array_rows(measured["arrays"])
                    state.paraview_message = f"ParaView {measured['version']} · {path.name}"
        except FileNotFoundError:
            if information_context["selection"] is selection:
                with state:
                    state.paraview_message = "ParaView 程序或源文件不可用"
        except TimeoutError:
            if information_context["selection"] is selection:
                with state:
                    state.paraview_message = "ParaView 信息读取超时，可以重试"
        except (OSError, ValueError) as error:
            if information_context["selection"] is selection:
                with state:
                    state.paraview_message = str(error)
        finally:
            information_context["running"] = False
            with state:
                state.paraview_busy = False

    def information_table(key, test_id):
        with twhtml.Dl(style="margin:0;display:grid;grid-template-columns:minmax(58px,auto) minmax(0,1fr);"
                            "column-gap:12px;row-gap:6px;font-size:13px;",
                        raw_attrs=[f'data-testid="{test_id}"']):
            with v3.Template(v_for=f"row in {key}", key="row.k"):
                twhtml.Dt("{{ row.k }}")
                twhtml.Dd("{{ row.v }}", raw_attrs=[':data-info-key="row.k"'],
                          style="overflow-wrap:anywhere;font-variant-numeric:tabular-nums;")

    def information_arrays(key, test_id):
        with twhtml.Details(v_if=(f"{key}.length",), style="margin-top:12px;",
                            raw_attrs=[f'data-testid="{test_id}"']):
            twhtml.Summary("数据数组（{{ " + key + ".length }}）", style="cursor:pointer;")
            with v3.Template(v_for=f"(row, i) in {key}", key="i"):
                with twhtml.Div(style="padding:7px 0;overflow-wrap:anywhere;"):
                    twhtml.Strong("{{ row.k }}")
                    twhtml.Div("{{ row.v }}", style="font-size:13px;font-variant-numeric:tabular-nums;")

    with SinglePageWithDrawerLayout(server, full_height=True) as layout:
        layout.title.style = "display:none"
        with layout:
            twclient.ClientTriggers(
                ref="vf_pipeline_preferences",
                mounted="() => { if (vf_pipeline_storage) { try { "
                        "trigger('vf_restore_selection', [window.localStorage.getItem(vf_pipeline_storage)]) "
                        "} catch (_) {} } }",
            )
            twclient.ClientStateChange(
                value="vf_saved_selection",
                change="() => { if (vf_pipeline_storage && vf_saved_selection) { try { "
                       "window.localStorage.setItem(vf_pipeline_storage, JSON.stringify(vf_saved_selection)) "
                       "} catch (_) {} } }",
            )
        with layout.toolbar:
            v3.VSelect(label="模式", v_model=("mode",), items=("mode_items",),
                       density="compact", hide_details=True, style="min-width:100px;max-width:130px")
            if times:
                v3.VSelect(label="时间步", v_model=("time_label",), items=("time_items",),
                           density="compact", hide_details=True, style="max-width:110px")
            v3.VSelect(label="染色", v_model=("field",), items=("field_items",),
                       density="compact", hide_details=True, style="min-width:86px;max-width:110px")
            # 模式相关参数（v_show 条件渲染）
            v3.VSelect(label="法向", v_model=("slice_axis",), items=("axis_items",),
                       v_show=("mode==='切面'",), density="compact", hide_details=True,
                       style="max-width:90px")
            v3.VSlider(label="位置", v_model=("slice_pos",), min=0.02, max=0.98, step=0.01,
                       v_show=("mode==='切面'",), density="compact", hide_details=True,
                       style="max-width:150px")
            v3.VTextField(label="等值（留空自动）", v_model=("iso_value",),
                          v_show=("['等值面', '表面等值线'].includes(mode)",), density="compact", hide_details=True,
                          style="max-width:130px")
            v3.VSelect(label="向量场", v_model=("vector_field",), items=("vector_items",), clearable=True,
                       v_show=("['流线', '矢量箭头'].includes(mode)",), density="compact", hide_details=True,
                       style="min-width:86px;max-width:124px")
            v3.VSlider(label="种子数", v_model=("seed_count",), min=5, max=200, step=1,
                       v_show=("mode==='流线'",), density="compact", hide_details=True,
                       style="max-width:140px")
            v3.VSlider(label="缩放", v_model=("glyph_scale",), min=0.2, max=5, step=0.1,
                       v_show=("mode==='矢量箭头'",), density="compact", hide_details=True,
                       style="max-width:140px")
            v3.VBtn("重置视角", click=lambda: (renderer.ResetCamera(),
                                               render_window.Render(),
                                               ctrl.view_update()),
                    variant="tonal")
            if times:
                v3.VBtn(
                    v3.VIcon("{{ playing ? 'mdi-pause' : 'mdi-play' }}"),
                    icon=True, density="compact", variant="tonal",
                    click="playing = !playing",
                    title="播放/暂停（时间步循环）",
                )
                v3.VBtn("导帧", click=lambda: _export_frames(), density="compact",
                        variant="tonal", title="逐时刻渲染出帧 → D-post/animations/")
            twhtml.Span("{{ scenes_status }}",
                        style="font-size:12px;align-self:center;margin-left:6px;min-width:0;"
                              "overflow:hidden;text-overflow:ellipsis;white-space:nowrap",
                        raw_attrs=['role="status"', 'data-testid="scene-script-status"'])
            v3.VSpacer()
        with layout.content:
            twhtml.Div("{{ filter_error }}", v_if=("filter_error",),
                       raw_attrs=['role="alert"', 'data-testid="viz-filter-error"'],
                       style="position:absolute;z-index:1;bottom:32px;left:12px;max-width:calc(100% - 24px);"
                             "padding:6px 10px;background:rgb(var(--v-theme-surface));color:rgb(var(--v-theme-on-surface));")
            view = twvtk.VtkRemoteView(render_window, interactive_ratio=1,
                                       style="width:100%;height:100%")
        # 右抽屉：VF_* 对象管线（显示开关+删除）
        with layout.drawer:
            with v3.VList(density="compact"):
                v3.VListSubheader("Sources / Filters")
                with v3.VListItem(density="compact"):
                    with twhtml.Div(style="display:flex;flex-wrap:wrap;gap:6px"):
                        for kind, label in (("surface", "表面"), ("slice", "切面"), ("contour", "表面等值线"),
                                            ("isosurface", "体等值面"), ("stream", "流线"), ("glyph", "矢量"),
                                            ("deform", "变形"), ("mode", "模态")):
                            v3.VBtn(label, click=f"vf_pending_add = '{kind}'", density="compact", variant="tonal",
                                    raw_attrs=[f'data-testid="viz-add-{kind}"'])
                with v3.VListItem(v_if=("analysis_error",), density="compact"):
                    twhtml.Div("{{ analysis_error }}", raw_attrs=['role="alert"', 'data-testid="viz-analysis-error"'])
                v3.VListSubheader("场景对象（scenes.py 管理）")
                with twhtml.Div(style="padding:0 12px 8px;"):
                    twhtml.Div(
                        "{{ vf_selection_count ? '已选 ' + vf_selection_count + ' 项' : '' }}",
                        raw_attrs=['role="status"', 'data-testid="vf-selection-count"'], style="font-size:13px;",
                    )
                    with twhtml.Div(raw_attrs=['role="tree"', 'aria-label="场景对象"', 'aria-multiselectable="true"']):
                        with v3.Template(v_for="group in vf_groups", key="group.kind"):
                            with twhtml.Details(raw_attrs=["open"], style="margin-top:8px;"):
                                twhtml.Summary("{{ group.label }}", style="font-weight:600;cursor:pointer;font-size:13px;")
                                with twhtml.Div(raw_attrs=['role="group"'], style="margin-top:2px;"):
                                    with v3.Template(v_for="it in group.items", key="it.name"):
                                        with twhtml.Div(
                                            raw_attrs=[':data-object-status="it.status"', 'data-testid="vf-pipeline-row"'],
                                            style="display:grid;grid-template-columns:minmax(0,1fr) auto auto;align-items:center;",
                                        ):
                                            with v3.VBtn(
                                                click="trigger('vf_pick', [it.name, $event.shiftKey ? 'range' : "
                                                      "($event.ctrlKey || $event.metaKey ? 'toggle' : 'single')])",
                                                color=("it.selected ? 'primary' : undefined",), variant="text",
                                                density="compact",
                                                style="justify-content:flex-start;text-transform:none;letter-spacing:normal;"
                                                      "padding:0 5px;overflow:hidden;min-width:0;",
                                                raw_attrs=[
                                                    'role="treeitem"', ':aria-selected="it.selected"',
                                                    ':aria-current="it.active ? \'true\' : null"',
                                                    ':title="it.name"', 'data-testid="vf-pipeline-object"',
                                                    """@contextmenu.prevent.stop="trigger('vf_context', [it.name, $event.clientX, $event.clientY])" """,
                                                    """@keydown.shift.f10.prevent.stop="trigger('vf_context', [it.name, $event.target.getBoundingClientRect().left, $event.target.getBoundingClientRect().bottom])" """,
                                                ],
                                            ):
                                                v3.VIcon("{{ it.active ? 'mdi-arrow-right-circle-outline' : 'mdi-cube-outline' }}",
                                                         size="16", style="margin-right:6px;")
                                                twhtml.Span("{{ it.name }}", style="overflow:hidden;text-overflow:ellipsis;")
                                            v3.VBtn(
                                                icon=("it.icon",), variant="text", size="x-small",
                                                click="trigger('vf_action', ['toggle', it.name])",
                                                disabled=("it.status === 'error'",),
                                                raw_attrs=[':aria-label="it.state_label + \' · \' + it.name"'],
                                            )
                                            v3.VBtn(
                                                icon="mdi-dots-vertical", variant="text", size="x-small",
                                                click="trigger('vf_context', [it.name, $event.currentTarget.getBoundingClientRect().left, "
                                                      "$event.currentTarget.getBoundingClientRect().bottom])",
                                                raw_attrs=[':aria-label="\'对象操作 · \' + it.name"'],
                                            )
                    with v3.VMenu(v_model=("vf_menu_open",), target=("vf_menu_point",),
                                 location="bottom start", close_on_content_click=True):
                        with v3.VList(density="compact", raw_attrs=['role="menu"', 'aria-label="所选对象操作"']):
                            for action, text in (("show", "显示所选"), ("hide", "隐藏所选"), ("isolate", "只显示所选"),
                                                 ("remove", "删除所选"), ("clear", "清空选择")):
                                v3.VListItem(title=text, click=f"trigger('vf_action', [{action!r}])",
                                             raw_attrs=['role="menuitem"'])
                v3.VListItem(v_if=("!vf_items.length",),
                             title="尚无场景对象")
                v3.VListSubheader("色图")
                with v3.VListItem(density="compact"):
                    v3.VSelect(label="预设", v_model=("cmap_name",), items=("cmap_items",),
                               density="compact", hide_details=True)
                with v3.VListItem(density="compact"):
                    with twhtml.Div(style="height:20px;border-radius:4px;overflow:hidden;"
                                          "background:repeating-conic-gradient(#cfd3df 0% 25%, #fff 0% 50%) 0 0 / 12px 12px;",
                                    raw_attrs=['role="img"', 'aria-label="当前色带"']):
                        twhtml.Div(style="height:100%;", raw_attrs=[':style="{ backgroundImage: cmap_preview }"',
                                                                    'data-testid="colormap-preview"'])
                with v3.VListItem(v_show=("cmap_name==='custom'",), density="compact"):
                    v3.VTextarea(label="控制点(JSON)", v_model=("cmap_points_text",), rows=4,
                                 density="compact", hide_details=True)
                with v3.VListItem(v_show=("cmap_name==='custom'",), density="compact"):
                    with twhtml.Div(style="display:grid;grid-template-columns:minmax(0,1fr) 116px;gap:8px;"):
                        v3.VSelect(label="颜色空间", v_model=("cmap_space",), items=("cmap_spaces",),
                                   density="compact", hide_details=True)
                        v3.VTextField(label="色阶（0 连续）", v_model=("cmap_levels",), type="number", min=0,
                                      max=256, density="compact", hide_details=True)
                with v3.VListItem(v_show=("cmap_name==='custom'",), density="compact"):
                    with twhtml.Div(style="display:flex;flex-wrap:wrap;gap:6px;"):
                        v3.VBtn("应用控制点", click=lambda: _apply_custom_points(), density="compact", variant="tonal")
                        v3.VBtn("应用映射设置", click=lambda: _apply_mapping_settings(), color="primary",
                                density="compact", variant="tonal")
                with v3.VListItem(v_show=("cmap_name==='custom'",), density="compact"):
                    with twhtml.Div(style="display:flex;flex-wrap:wrap;gap:6px;"):
                        v3.VBtn("默认", click=lambda: _reset_custom_points(), density="compact", variant="tonal")
                        v3.VBtn("导入", click=lambda: _import_cmap('json'), density="compact", variant="tonal", title="导入 JSON 色图")
                        v3.VBtn("导出", click=lambda: _export_cmap('json'), density="compact", variant="tonal", title="导出 JSON 色图")
                with v3.VListItem(v_show=("cmap_name==='custom'",), density="compact"):
                    with twhtml.Div(style="display:grid;justify-content:start;gap:6px;"):
                        v3.VBtn("导入 ParaView XML", click=lambda: _import_cmap('xml'), density="compact", variant="tonal")
                        v3.VBtn("导出 ParaView XML", click=lambda: _export_cmap('xml'), density="compact", variant="tonal",
                                title="交换颜色控制点，完整映射设置请使用 JSON")
                with v3.VListItem(v_if=("cmap_status",), density="compact"):
                    twhtml.Span("{{ cmap_status }}", style="font-size:12px;overflow-wrap:anywhere;",
                                raw_attrs=['role="status"', 'data-testid="colormap-status"'])
                with v3.VListItem(density="compact"):
                    v3.VCheckbox(label="反转", v_model=("cmap_invert",),
                                 density="compact", hide_details=True)
                    v3.VCheckbox(label="对数轴", v_model=("cmap_log",),
                                 density="compact", hide_details=True)
                with v3.VListItem(density="compact"):
                    v3.VTextField(label="范围 min（0=自动）", v_model=("cmap_min",),
                                  density="compact", hide_details=True, type="number")
                with v3.VListItem(density="compact"):
                    v3.VTextField(label="范围 max（0=自动）", v_model=("cmap_max",),
                                  density="compact", hide_details=True, type="number")
                with v3.VListItem(density="compact"):
                    v3.VSlider(label="全局不透明度", v_model=("cmap_opacity",),
                               min=0.05, max=1, step=0.05, density="compact", hide_details=True)
                v3.VListSubheader("相机轨道")
                with v3.VListItem(density="compact"):
                    v3.VTextarea(label="关键帧(JSON)", v_model=("camera_track_text",), rows=4,
                                 density="compact", hide_details=True)
                with v3.VListItem(density="compact"):
                    v3.VBtn("应用轨道", click=lambda: _apply_camera_track(), color="primary",
                            density="compact", variant="tonal")
                    v3.VBtn("播放轨道", click="camera_playing = true", density="compact", variant="tonal")
                    v3.VBtn("暂停", click="camera_playing = false", density="compact", variant="tonal")
                    v3.VBtn("导出轨道", click=lambda: _export_camera_track(), density="compact", variant="tonal")
                    v3.VCheckbox(label="循环", v_model=("camera_loop",), density="compact", hide_details=True)
                with v3.VListItem(v_if=("camera_status",), density="compact"):
                    twhtml.Span("{{ camera_status }}", style="font-size:11px;color:#8b93a7")
                v3.VListSubheader("视角")
                with v3.VListItem(density="compact"):
                    v3.VBtn("+X", click=lambda: _camera_preset("+X"), density="compact", variant="tonal")
                    v3.VBtn("+Y", click=lambda: _camera_preset("+Y"), density="compact", variant="tonal")
                    v3.VBtn("+Z", click=lambda: _camera_preset("+Z"), density="compact", variant="tonal")
                    v3.VBtn("等轴测", click=lambda: _camera_preset("iso"), density="compact", variant="tonal")
                with v3.VListItem(density="compact"):
                    v3.VBtn("保存视角", click=lambda: _save_view(), density="compact", variant="tonal")
                    v3.VBtn("恢复视角", click=lambda: _restore_view(), density="compact", variant="tonal")
                    v3.VBtn("截图", click=lambda: _shot_view(), density="compact", variant="tonal")
                with v3.VListItem(density="compact"):
                    v3.VTextField(label="书签名称", v_model=("camera_bookmark_name",), density="compact",
                                  error_messages=("camera_bookmark_error",), hide_details="auto",
                                  raw_attrs=["@keydown.enter.prevent=\"trigger('camera_save_bookmark')\""])
                with v3.VListItem(density="compact"):
                    v3.VBtn("保存书签", click=lambda: _save_camera_bookmark(), density="compact", variant="tonal")
                    v3.VBtn("撤回视角", click=lambda: _undo_camera(), disabled=("!camera_can_undo",),
                            density="compact", variant="tonal")
                with v3.VListItem(density="compact"):
                    v3.VSelect(label="视角书签", v_model=("camera_bookmark_selected",),
                               items=("camera_bookmark_names",), density="compact", hide_details=True)
                with v3.VListItem(density="compact"):
                    v3.VBtn("应用书签", click=lambda: _apply_camera_bookmark(),
                            disabled=("!camera_bookmark_names.length",), density="compact", variant="tonal")
                    v3.VBtn("重读书签", click=lambda: _load_camera_views(notify=True), density="compact", variant="tonal")
                with v3.VListItem(density="compact"):
                    v3.VBtn("保留截图", click=lambda: _shot_view(retain=True), density="compact", variant="tonal")
                with v3.VListItem(v_if=("camera_view_status",), density="compact"):
                    twhtml.Span("{{ camera_view_status }}", raw_attrs=['role="status"', 'data-testid="viz-camera-status"'],
                                style="font-size:11px;color:#8b93a7;overflow-wrap:anywhere")
                with v3.VListItem(density="compact"):
                    v3.VBtn("内存检查器", click="viz_memory_open = !viz_memory_open", density="compact",
                            variant="tonal", raw_attrs=[':aria-expanded="viz_memory_open"'])
                with v3.VListItem(v_if=("viz_memory_open",), density="compact"):
                    with twhtml.Section(raw_attrs=['aria-label="实时内存"']):
                        information_table("viz_memory_rows", "viz-memory-rows")
                        twhtml.P("{{ viz_memory_status }}", raw_attrs=['role="status"'])
                        v3.VCheckbox(label="随管线更新", v_model=("viz_memory_auto",), density="compact", hide_details=True)
                        v3.VBtn("刷新内存", click=lambda: refresh_memory(), density="compact", variant="tonal")
                v3.VListSubheader("VF Python 控制台")
                with v3.VListItem(density="compact"):
                    v3.VTextField(label="VF_函数(字面量参数)", v_model=("console_input",),
                                  density="compact", hide_details=True)
                    v3.VBtn("执行", click=lambda: _console_execute(), color="primary",
                            density="compact", variant="tonal")
                with v3.VListItem(v_if=("console_output",), density="compact"):
                    twhtml.Span("{{ console_output }}", style="font-size:11px;color:#8b93a7;white-space:pre-wrap")
                # Inspector：选中对象的参数表单（按类型出件，Apply 即重建）
                with v3.Template(v_if=("vf_selected",)):
                    v3.VListSubheader("属性 · {{ vf_selected }}")
                    with v3.VListItem(v_if=("vf_items.find(x => x.name === vf_selected)?.kind !== 'mode'",)):
                        v3.VSelect(label="染色场", v_model=("edit_field",),
                                   items=("edit_scalar_items",), density="compact",
                                   hide_details=True)
                    with v3.VListItem(v_if=("['stream','glyph','deform'].includes(vf_items.find(x => x.name === vf_selected)?.kind)",)):
                        v3.VSelect(label="向量场", v_model=("edit_vector",), items=("edit_vector_items",),
                                   clearable=True, density="compact", hide_details=True)
                    with v3.Template(v_if=("vf_items.find(x => x.name === vf_selected)?.kind === 'mode'",)):
                        with v3.VListItem():
                            v3.VSelect(label="模态场", v_model=("edit_mode_field",), items=("edit_mode_items",),
                                       density="compact", hide_details=True,
                                       raw_attrs=['data-testid="viz-mode-field"'])
                        with v3.VListItem():
                            v3.VSelect(label="虚部场（可选）", v_model=("edit_imaginary",), items=("edit_mode_items",),
                                       clearable=True, density="compact", hide_details=True)
                        with v3.VListItem():
                            v3.VTextField(label="相位（°）", v_model=("edit_phase",), type="number",
                                          density="compact", hide_details=True,
                                          raw_attrs=['data-testid="viz-mode-phase"'])
                    with v3.VListItem(v_if=("['mode','deform'].includes(vf_items.find(x => x.name === vf_selected)?.kind)",)):
                        v3.VTextField(label="倍率", v_model=("edit_scale",), type="number",
                                      density="compact", hide_details=True,
                                      raw_attrs=['data-testid="viz-displacement-scale"'])
                    with v3.Template(v_if=("vf_items.find(x => x.name === vf_selected)?.kind === 'slice'",)):
                        with v3.VListItem():
                            v3.VSelect(label="法向", v_model=("edit_axis",),
                                       items=("axis_items",), density="compact",
                                       hide_details=True)
                        with v3.VListItem():
                            v3.VSlider(label="位置", v_model=("edit_pos",),
                                       min=0.02, max=0.98, step=0.01,
                                       density="compact", hide_details=True)
                    with v3.Template(v_if=("vf_items.find(x => x.name === vf_selected)?.kind === 'contour'",)):
                        with v3.VListItem():
                            v3.VTextField(label="等值（0=自动）", v_model=("edit_iso",),
                                          density="compact", hide_details=True)
                    with v3.VListItem(v_if=("vf_items.find(x => x.name === vf_selected)?.kind === 'isosurface'",)):
                        v3.VTextField(label="等值（逗号分隔）", v_model=("edit_iso",),
                                      density="compact", hide_details=True,
                                      raw_attrs=['data-testid="viz-iso-values"'])
                    with v3.Template(v_if=("vf_items.find(x => x.name === vf_selected)?.kind === 'stream'",)):
                        with v3.VListItem():
                            v3.VSlider(label="种子数", v_model=("edit_seeds",),
                                       min=5, max=200, step=1, density="compact",
                                       hide_details=True)
                    with v3.Template(v_if=("vf_items.find(x => x.name === vf_selected)?.kind === 'glyph'",)):
                        with v3.VListItem():
                            v3.VSlider(label="缩放", v_model=("edit_scale",),
                                       min=0.2, max=5, step=0.1, density="compact",
                                       hide_details=True)
                    with v3.VListItem():
                        v3.VBtn("应用", click="vf_apply_tick = (vf_apply_tick || 0) + 1", color="primary",
                                density="compact", block=True, raw_attrs=['data-testid="viz-apply-filter"'])
                    with v3.VListItem(v_if=("edit_error",), density="compact"):
                        twhtml.Div("{{ edit_error }}", raw_attrs=['role="alert"'])
                v3.VListSubheader("数据信息")
                with v3.VListItem(v_if=("result_block_items.length",), density="compact"):
                    v3.VSelect(label="结果数据块", v_model=("result_block",), items=("result_block_items",),
                               density="compact", hide_details=True)
                with v3.VListItem(v_if=("result_block_status",), density="compact"):
                    twhtml.Span("{{ result_block_status }}", raw_attrs=['role="status"', 'data-testid="viz-result-block-status"'])
                with twhtml.Section(style="padding:0 16px 14px;"):
                    twhtml.P("{{ info_name }}", style="font-weight:600;margin:0 0 8px;overflow-wrap:anywhere;",
                             raw_attrs=['data-testid="viz-info-selection"'])
                    information_table("info_rows", "viz-information")
                    information_arrays("info_arrays", "viz-info-arrays")
                    v3.VBtn("读取 ParaView 源数据", click=read_paraview_information, disabled=("paraview_busy",),
                            size="small", variant="tonal", style="margin:14px 0 6px;",
                            raw_attrs=['data-testid="viz-paraview-information"'])
                    twhtml.P("{{ paraview_message }}", raw_attrs=['role="status"', 'data-testid="viz-paraview-status"'],
                             style="overflow-wrap:anywhere;margin:0 0 8px;font-size:13px;")
                    with v3.Template(v_if=("paraview_rows.length",)):
                        information_table("paraview_rows", "viz-paraview-data")
                        information_arrays("paraview_arrays", "viz-paraview-arrays")

    ctrl.view_update = view.update

    # ================================================================
    # scenes.py 监听执行（D 阶段自动化核心；语义对齐原版 Studio：
    # 监听 D-post/scenes.py 变更 → 在当前可视化会话执行 → VF_* 增量管线）
    # 日志契约：D-post/logs/viz_executor.log（原版为 paraview_executor.log，同理）
    # ================================================================
    import asyncio
    import time as _time
    import traceback as _tb

    vf_objects: dict[str, dict] = {}   # VF_* 场景对象注册表 {name: {spec, mapper, actor, live}}
    vf_scene = {"reader": None, "grid": None, "bounds": None, "diag": 1.0,
                "fields": [], "times": [], "case_rel": ""}
    scenes_path = case_root / "D-post" / "scenes.py"
    viz_log_path = case_root / "D-post" / "logs" / "viz_executor.log"

    def _viz_log(msg: str):
        viz_log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(viz_log_path, "a", encoding="utf-8") as f:
            f.write(f"[{_time.strftime('%H:%M:%S')}] {msg}\n")

    def _sync_vf_state():
        """Project rendered object state and selection into the pipeline tree."""
        labels = {"surface": "表面", "slice": "切面", "contour": "表面等值线", "isosurface": "体等值面",
                  "stream": "流线", "glyph": "矢量箭头", "deform": "变形", "mode": "模态"}
        statuses = {
            "visible": ("mdi-eye-outline", "可见"),
            "hidden": ("mdi-eye-off-outline", "隐藏"),
            "empty": ("mdi-checkbox-blank-circle-outline", "无输出"),
            "error": ("mdi-alert-circle-outline", "构建失败"),
        }
        items, groups = [], {}
        for name in sorted(vf_objects, key=lambda item: (vf_objects[item]["spec"]["kind"], item)):
            ent = vf_objects[name]
            kind = ent["spec"]["kind"]
            actor = ent.get("actor")
            output = ent["mapper"].GetInput() if actor is not None else None
            if actor is None or ent.get("error"):
                status = "error"
            elif not actor.GetVisibility():
                status = "hidden"
            else:
                status = "visible" if output and output.GetNumberOfCells() else "empty"
            icon, label = statuses[status]
            item = {"name": name, "kind": kind, "status": status, "icon": icon, "state_label": label,
                    "selected": name in selection_names, "active": name == state.vf_selected}
            items.append(item)
            groups.setdefault(kind, {"kind": kind, "label": labels.get(kind, kind), "items": []})["items"].append(item)
        state.vf_items = items
        state.vf_groups = list(groups.values())

    def _build_lut(rng):
        import matplotlib.pyplot as plt
        lo, hi = rng
        try:
            cmin, cmax = float(state.cmap_min or 0), float(state.cmap_max or 0)
        except (TypeError, ValueError):
            cmin, cmax = 0.0, 0.0
        if math.isfinite(cmin) and math.isfinite(cmax) and cmax > cmin:
            lo, hi = cmin, cmax
        if state.cmap_log:
            lo = max(lo, 1e-30)
            hi = hi if hi > lo else max(1.0, lo * 2)
        document = state.cmap_document
        bands = 0
        if state.cmap_name == "custom":
            colors = sample_colormap(document, state.cmap_invert)
            bands = document.get("levels", 0)
        else:
            cmap = plt.get_cmap(state.cmap_name)
            colors = [cmap(1.0 - i / 255 if state.cmap_invert else i / 255) for i in range(256)]
        lut = vtk.vtkLookupTable()
        lut.SetRange(lo, hi)
        lut.SetNumberOfTableValues(len(colors))
        for i, color in enumerate(colors):
            lut.SetTableValue(i, *color)
        if state.cmap_name == "custom" and document.get("nan_color"):
            hex_color = document["nan_color"]
            lut.SetNanColor(*(int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)), 1.0)
        if state.cmap_log:
            lut.SetScaleToLog10()
        lut.Build()
        return lut, (lo, hi), bands

    def _apply_mapper_colormap(target, limits, *, preview=False):
        lut, limits, bands = _build_lut(limits)
        target.SetLookupTable(lut)
        target.SetScalarRange(limits)
        # Vertex interpolation would blend distinct bins on scalar gradients.
        target.SetInterpolateScalarsBeforeMapping(bool(bands))
        if preview:
            stops = []
            count = lut.GetNumberOfTableValues()
            for index in range(count) if bands else [*range(0, count - 1, 16), count - 1]:
                r, g, b, alpha = lut.GetTableValue(index)
                color = f"rgba({round(r * 255)}, {round(g * 255)}, {round(b * 255)}, " \
                        f"{alpha * float(state.cmap_opacity):.3f})"
                if bands:
                    stops.append(f"{color} {index / count:.2%} {(index + 1) / count:.2%}")
                else:
                    stops.append(f"{color} {index / (count - 1):.2%}")
            state.cmap_preview = "linear-gradient(to right, " + ", ".join(stops) + ")"

    def _apply_cmap_all():
        _apply_mapper_colormap(mapper, _last_rng[0] if _last_rng else (0.0, 1.0), preview=True)
        opacity = float(state.cmap_opacity)
        actor.GetProperty().SetOpacity(opacity)
        for ent in vf_objects.values():
            if "range" in ent:
                _apply_mapper_colormap(ent["mapper"], ent["range"])
                ent["actor"].GetProperty().SetOpacity(opacity)
        render_window.Render()
        ctrl.view_update()

    DEFAULT_CUSTOM_POINTS = [
        {"position": 0, "color": "#0000FF"},
        {"position": 0.5, "color": "#FFFFFF"},
        {"position": 1, "color": "#FF0000"},
    ]

    def _set_colormap(document):
        state.cmap_document = document
        state.cmap_points_text = colormap_json(document)
        state.cmap_space = document.get("space", "RGB")
        state.cmap_levels = document.get("levels", 0)
        state.cmap_name = "custom"
        _apply_cmap_all()

    def _apply_custom_points():
        try:
            document = normalize_colormap(state.cmap_points_text)
            _set_colormap(document)
            state.cmap_status = f"已应用 {len(document['points'])} 个控制点"
            return document
        except ColormapError as error:
            state.cmap_status = f"控制点错误: {error}"
            return None

    def _apply_mapping_settings():
        try:
            requested = normalize_colormap(state.cmap_points_text)
            levels = str(state.cmap_levels or 0)
            if not levels.isascii() or not levels.isdecimal() or len(levels) > 3:
                raise ColormapError("色阶数须为 0（连续）或 2–256")
            document = normalize_colormap({**requested, "schema": ADVANCED_SCHEMA,
                                           "space": state.cmap_space, "levels": int(levels)})
            _set_colormap(document)
            state.cmap_status = "已应用映射设置"
        except ColormapError as error:
            state.cmap_status = f"映射设置错误: {error}"

    def _colormap_path(kind):
        path = case_root / "D-post" / f"colormap.{kind}"
        if not path.resolve().is_relative_to(case_root.resolve()):
            raise ColormapError("色图文件必须位于当前算例内")
        return path

    def _export_cmap(kind):
        from tempfile import NamedTemporaryFile

        document = _apply_custom_points()
        if document is None:
            return
        temporary = None
        try:
            path = _colormap_path(kind)
            data = paraview_colormap_xml(document) if kind == 'xml' else colormap_json(document)
            path.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
            temporary.replace(path)
            state.cmap_status = f"已导出 D-post/colormap.{kind}"
        except (OSError, ColormapError) as error:
            state.cmap_status = f"导出失败: {error}"
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _import_cmap(kind):
        try:
            path = _colormap_path(kind)
            with path.open('rb') as stream:
                data = stream.read(MAX_COLORMAP_BYTES + 1)
            if len(data) > MAX_COLORMAP_BYTES:
                raise ColormapError("色图文件超过 256 KiB")
            document = parse_paraview_colormap(data) if kind == 'xml' else normalize_colormap(data.decode('utf-8-sig'))
            _set_colormap(document)
            state.cmap_status = f"已导入 D-post/colormap.{kind}"
        except (OSError, UnicodeError, ColormapError) as error:
            state.cmap_status = f"导入失败: {error}"

    def _reset_custom_points():
        _set_colormap(normalize_colormap(DEFAULT_CUSTOM_POINTS))
        state.cmap_status = "已恢复默认控制点"

    _last_rng: list = []     # 最近一次着色的数据范围（rebuild 里更新）

    def range_text(values):
        return f"[{values[0]:.6g}, {values[1]:.6g}]"

    def information_rows(summary, time_value):
        rows = [("网格类型", summary.get("composite") or summary["dataset"]),
                ("点", f"{summary['points']:,}"), ("单元", f"{summary['cells']:,}"),
                ("VTK 内存", f"{summary['memory_kib']:,} KiB"),
                ("时刻", f"{time_value:g}" if time_value is not None else "静态数据")]
        if summary.get("datasets") is not None:
            rows.append(("数据块", f"{summary['datasets']:,}"))
        if summary.get("time_steps"):
            rows.append(("时间步", f"{summary['time_steps']:,}"))
        if summary.get("bounds"):
            rows.extend((f"范围 {axis}", range_text(values)) for axis, values in zip("XYZ", summary["bounds"]))
        if summary.get("cell_types"):
            rows.append(("单元类型", "、".join(summary["cell_types"])))
        for key, label in (("dimensions", "维数"), ("origin", "原点"), ("spacing", "间距")):
            if key in summary:
                rows.append((label, " × ".join(f"{value:g}" for value in summary[key])))
        return [{"k": key, "v": value} for key, value in rows]

    def array_rows(arrays):
        rows = []
        for item in arrays:
            label = f"{item['name']}（{item['association']}）"
            values = [item["type"], f"{item['components']} 分量", f"{item['tuples']:,} 元组"]
            if item["bytes"] is not None:
                values.append(f"{item['bytes']:,} B")
            if item.get("allocated_kib") is not None:
                values.append(f"VTK 分配 {item['allocated_kib']:,} KiB")
            if item["range"] is not None:
                measure = "幅值" if item["components"] > 1 else "范围"
                values.append(f"{measure} {range_text(item['range'])}")
            rows.append({"k": label, "v": " · ".join(values)})
        return rows

    def _sync_info(grid=None, *, time_value=None, path=None):
        if grid is not None:
            information_context["grid"] = grid
            selected = ((path or source_path).resolve(), selected_time() if time_value is None else time_value)
            if information_context["selection"] != selected:
                information_context["selection"] = selected
                state.paraview_rows = []
                state.paraview_arrays = []
                state.paraview_message = ""
        source = information_context["grid"]
        if source is None:
            return
        selected_filter = vf_objects.get(state.vf_selected)
        if selected_filter and selected_filter.get("mapper"):
            source = selected_filter["mapper"].GetInput()
            state.info_name = state.vf_selected
        else:
            state.info_name = information_context["selection"][0].name
        if source is None:
            state.info_rows = []
            state.info_arrays = []
            return
        stamp = source.GetMTime()
        if information_context.get("measured") is not source or information_context.get("stamp") != stamp:
            information_context.update(measured=source, stamp=stamp, summary=dataset_information(source))
        summary = information_context["summary"]
        state.info_rows = information_rows(summary, information_context["selection"][1])
        state.info_arrays = array_rows(summary["arrays"])

    def _vf_render():
        renderer.ResetCameraClippingRange()
        render_window.Render()
        ctrl.view_update()

    def _load_from_reader(r, t):
        if t is not None:
            r.UpdateTimeStep(t)
        else:
            r.Update()
        out = r.GetOutput()
        block = out.GetBlock(0) if out.GetNumberOfBlocks() else None
        calc = vtk.vtkArrayCalculator()
        calc.SetInputData(block)
        flds = [r.GetCellArrayName(i) for i in range(r.GetNumberOfCellArrays())]
        has_u = "U" in filters.field_names(block, (3,)) and block.GetPointData().HasArray("U")
        if has_u:
            calc.AddVectorArrayName("U")
            calc.SetFunction("mag(U)")
            calc.SetResultArrayName("Umag")
            calc.SetAttributeTypeToPointData()
        g = vtk.vtkUnstructuredGrid()
        if has_u:
            calc.Update()
            g.DeepCopy(calc.GetOutput())
            g.GetPointData().SetActiveVectors("U")
        else:
            g.DeepCopy(block)
        scalars = [n for n in flds
                   if (a := g.GetPointData().GetArray(n)) is not None
                   and a.GetNumberOfComponents() == 1]
        if has_u:
            scalars.append("Umag")
        return g, scalars or ["p"]

    def _load_generic_result(t):
        result = load_result_file(source_path, t)
        grid = result.dataset
        vector = grid.GetPointData().GetArray("U")
        if vector is not None and vector.GetNumberOfComponents() == 3:
            calc = vtk.vtkArrayCalculator()
            calc.SetInputData(grid)
            calc.AddVectorArrayName("U")
            calc.SetFunction("mag(U)")
            calc.SetResultArrayName("Umag")
            calc.SetAttributeTypeToPointData()
            calc.Update()
            output = calc.GetOutput()
            grid = output.NewInstance()
            grid.DeepCopy(output)
            grid.GetPointData().SetActiveVectors("U")
        return result, grid

    def vf_load(rel_dir: str = ""):
        """载入求解目录为当前场景数据源（缺省=本会话启动时的目录）。"""
        rd = (case_root / rel_dir.strip("/")) if rel_dir else source_path.parent
        if not rel_dir and args.data_file:
            result, g = _load_generic_result(None)
            b = g.GetBounds()
            d = ((b[1]-b[0])**2 + (b[3]-b[2])**2 + (b[5]-b[4])**2) ** 0.5 or 1.0
            fields = [array.GetName() for i in range(g.GetPointData().GetNumberOfArrays())
                      if (array := g.GetPointData().GetArray(i)) is not None and array.GetNumberOfComponents() == 1]
            vf_scene.update(reader="generic", grid=g, bounds=b, diag=d, fields=fields,
                            times=result.times, information_path=source_path, case_rel=str(source_path.relative_to(case_root)))
            _viz_log(f"VF_load {vf_scene['case_rel']}: {g.GetNumberOfPoints()} 点, "
                     f"times={result.times}, fields={fields}")
            return "VF_file"
        if not (rd / "system" / "controlDict").is_file():
            raise ValueError(f"{rd} 不是有效 OpenFOAM 算例目录")
        foam_f = rd / f"{rd.name}.foam"
        foam_f.touch(exist_ok=True)
        r = vtk.vtkOpenFOAMReader()
        r.SetFileName(str(foam_f))
        r.CreateCellToPointOn()
        r.UpdateInformation()
        r.EnableAllCellArrays()
        tv = r.GetTimeValues()
        ts = [tv.GetValue(i) for i in range(tv.GetNumberOfValues())]
        g, flds = _load_from_reader(r, ts[-1] if ts else None)
        b = g.GetBounds()
        d = ((b[1]-b[0])**2 + (b[3]-b[2])**2 + (b[5]-b[4])**2) ** 0.5 or 1.0
        vf_scene.update(reader=r, grid=g, bounds=b, diag=d, fields=flds,
                        times=ts, information_path=foam_f, case_rel=str(rd.relative_to(case_root)))
        _viz_log(f"VF_load {vf_scene['case_rel']}: {g.GetNumberOfPoints()} 点, "
                 f"times={ts}, fields={flds}")
        return "VF_case"

    def _vf_field(name):
        field = vf_objects[name]["spec"].get("field")
        kind = vf_objects[name]["spec"]["kind"]
        if kind in ("stream", "glyph") and field in filters.field_names(vf_scene["grid"], (3,)):
            field = f"{field}mag" if vf_scene["grid"].GetPointData().HasArray(f"{field}mag") else "几何"
        return field or (
            "Umag" if "Umag" in vf_scene["fields"] else
            (vf_scene["fields"][0] if vf_scene["fields"] else "几何"))

    def _vf_build(name):
        """按 spec 构建/重建对象输出端口（filter 保活在注册表里）"""
        spec = vf_objects[name]["spec"]
        grid = vf_scene["grid"]
        b = vf_scene["bounds"]
        d = vf_scene["diag"]
        field = _vf_field(name)
        arr = grid.GetPointData().GetArray(field)
        rng = arr.GetRange() if arr else (0.0, 1.0)
        if rng[1] - rng[0] < 1e-30:
            rng = (rng[0] - 0.5, rng[1] + 0.5)   # 退化范围防 LUT 塌缩
        kind = spec["kind"]
        live = []
        association = spec.get("association", "auto")
        if kind in {"stream", "glyph", "deform"}:
            legacy = spec.get("field")
            names = filters.field_names(grid, (3,))
            requested = spec.get("vector") or (legacy if kind in ("stream", "glyph") and legacy in names else None)
            vector = requested if association != "auto" and requested else filters.vector_field(
                grid, requested, deformation=kind == "deform")
            if kind != "deform":
                grid, _ = filters.point_field(vtk, grid, vector, (3,), association=association)
        if kind == "slice":
            ax = AXES.get(spec.get("axis", "Z"), 2)
            lo, hi = b[2*ax], b[2*ax+1]
            origin = [0.0, 0.0, 0.0]; origin[ax] = lo + (hi-lo)*float(spec.get("pos", 0.5))
            normal = [0.0, 0.0, 0.0]; normal[ax] = 1.0
            plane = vtk.vtkPlane(); plane.SetOrigin(origin); plane.SetNormal(normal)
            f = vtk.vtkCutter(); f.SetCutFunction(plane); f.SetInputData(grid)
            live.extend([plane, f])
        elif kind in {"contour", "isosurface"}:
            iso = spec.get("iso")
            if kind == "contour" and iso == 0:
                iso = None
            live, rng = filters.contour(vtk, grid, field, iso, volume=kind == "isosurface",
                                        radius=d*0.002, association=association)
        elif kind == "deform":
            live = filters.displacement(vtk, grid, vector, spec.get("scale", 1), association=association)
        elif kind == "mode":
            live, field, rng = filters.mode_shape(vtk, grid, spec["mode_field"], spec.get("scale", 1),
                                                   spec.get("phase", 0), imaginary=spec.get("imaginary"),
                                                   association=association)
        elif kind == "stream":
            cx = (b[0]+b[1])/2; cy = (b[2]+b[3])/2; cz = (b[4]+b[5])/2
            line = vtk.vtkLineSource()
            line.SetPoint1(cx-(b[1]-b[0])*0.4, cy, cz)
            line.SetPoint2(cx+(b[1]-b[0])*0.4, cy, cz)
            line.SetResolution(max(2, int(spec.get("seeds", 40))))
            st = vtk.vtkStreamTracer(); st.SetInputData(grid)
            st.SetSourceConnection(line.GetOutputPort())
            st.SetInputArrayToProcess(0, 0, 0, vtk.vtkDataObject.FIELD_ASSOCIATION_POINTS, vector)
            st.SetIntegratorTypeToRungeKutta2(); st.SetIntegrationDirectionToBoth()
            st.SetMaximumPropagation(d*5); st.SetInitialIntegrationStep(d/200)
            st.SetMinimumIntegrationStep(d/2000); st.SetMaximumNumberOfSteps(2000)
            f = vtk.vtkTubeFilter(); f.SetInputConnection(st.GetOutputPort())
            f.SetRadius(d*0.003); f.SetNumberOfSides(6)
            live.extend([line, st, f])
        elif kind == "glyph":
            arrow = vtk.vtkArrowSource()
            mask = vtk.vtkMaskPoints(); mask.SetInputData(grid)
            mask.SetOnRatio(max(1, grid.GetNumberOfPoints()//200)); mask.RandomModeOff()
            f = vtk.vtkGlyph3D(); f.SetInputConnection(mask.GetOutputPort())
            f.SetSourceConnection(arrow.GetOutputPort())
            f.SetInputArrayToProcess(1, 0, 0, vtk.vtkDataObject.FIELD_ASSOCIATION_POINTS, vector)
            f.SetVectorModeToUseVector(); f.SetScaleModeToScaleByVector()
            f.SetScaleFactor(d*0.08*float(spec.get("scale", 1.0))); f.OrientOn()
            live.extend([arrow, mask, f])
        elif kind == "surface":
            f = vtk.vtkGeometryFilter(); f.SetInputData(grid)
            live.append(f)
        else:
            raise ValueError(f"未知对象类型 {kind}")
        if rng[1] - rng[0] < 1e-30:
            rng = (rng[0] - 0.5, rng[1] + 0.5)
        return live, rng, field

    def _vf_apply(name):
        """增量语义：同名存在→重建 filter 换参数（actor/mapper 身份不变）；不存在→建 actor"""
        live, rng, field = _vf_build(name)
        ent = vf_objects[name]
        if "actor" not in ent:
            ent["mapper"] = vtk.vtkPolyDataMapper()
            ent["mapper"].SetScalarModeToUsePointFieldData()
            ent["mapper"].SetScalarVisibility(0 if field == "几何" else 1)
            ent["actor"] = vtk.vtkActor()
            ent["actor"].SetMapper(ent["mapper"])
            ent["actor"].GetProperty().SetLineWidth(2)
            renderer.AddActor(ent["actor"])
        ent["mapper"].SetInputConnection(live[-1].GetOutputPort())
        ent["live"] = live
        ent["mapper"].SetScalarVisibility(field != "几何")
        ent["mapper"].SelectColorArray(field)
        ent["range"] = rng
        _apply_mapper_colormap(ent["mapper"], rng)
        ent["actor"].GetProperty().SetOpacity(float(state.cmap_opacity))
        ent["mapper"].Update()
        if name == state.vf_selected:
            _sync_info()

    # ---------- 脚本 facade（scenes.py 里的裸函数；自有 API，语义对齐 VF_* 契约）----------
    def _create_vf_object(name, spec):
        ent = vf_objects.setdefault(name, {})
        last_spec = ent.get("spec")
        ent["spec"] = spec
        try:
            _vf_apply(name)
        except Exception as error:
            if last_spec is not None:
                ent["spec"] = last_spec
            else:
                vf_objects.pop(name, None)
            _sync_vf_state()
            raise
        ent.pop("error", None)
        _restore_available_selection()
        _sync_vf_state()
        return name

    def vf_slice(name="VF_slice", field=None, axis="Z", pos=0.5):
        key = _create_vf_object(name, {"kind": "slice", "field": field, "axis": axis, "pos": pos})
        _viz_log(f"+{name} slice {field or ''} {axis}@{pos}")
        return key

    def vf_contour(name="VF_contour", field=None, iso=0.0):
        key = _create_vf_object(name, {"kind": "contour", "field": field, "iso": iso})
        _viz_log(f"+{name} contour {field or ''} iso={iso}")
        return key

    def vf_isosurface(name="VF_isosurface", field=None, values=None, *, association="auto"):
        return _create_vf_object(name, {"kind": "isosurface", "field": field, "iso": values, "association": association})

    def vf_deform(name="VF_deform", vector=None, scale=1.0, *, field=None, association="auto"):
        return _create_vf_object(name, {"kind": "deform", "vector": vector, "scale": scale,
                                       "field": field, "association": association})

    def vf_mode(name="VF_mode", field=None, scale=1.0, phase=0.0, *, imaginary=None, association="auto"):
        return _create_vf_object(name, {"kind": "mode", "mode_field": field, "scale": scale, "phase": phase,
                                       "imaginary": imaginary, "association": association})

    def vf_stream(name="VF_stream", field=None, seeds=40, *, vector=None, association="auto"):
        key = _create_vf_object(name, {"kind": "stream", "field": field, "seeds": seeds,
                                      "vector": vector, "association": association})
        _viz_log(f"+{name} stream seeds={seeds}")
        return key

    def vf_glyph(name="VF_glyph", field=None, scale=1.0, *, vector=None, association="auto"):
        key = _create_vf_object(name, {"kind": "glyph", "field": field, "scale": scale,
                                      "vector": vector, "association": association})
        _viz_log(f"+{name} glyph scale={scale}")
        return key

    def vf_surface(name="VF_surface", field=None):
        key = _create_vf_object(name, {"kind": "surface", "field": field})
        _viz_log(f"+{name} surface {field or ''}")
        return key

    def vf_color(name, field):
        if name in vf_objects:
            _create_vf_object(name, {**vf_objects[name]["spec"], "field": field})
            _viz_log(f"~{name} color {field}")

    def _remove_vf_actor(name):
        ent = vf_objects.pop(name, None)
        if ent is not None:
            if "actor" in ent:
                renderer.RemoveActor(ent["actor"])
            _viz_log(f"-{name}")

    def vf_remove(name):
        _remove_vf_actor(name)
        if name in selection_names:
            _set_selection(selection_names, state.vf_selected, selection_anchor)
        else:
            _sync_vf_state()

    def vf_time(t: float):
        if not vf_scene["times"]:
            return
        t = min(vf_scene["times"], key=lambda x: abs(x - float(t)))
        if vf_scene["reader"] == "generic":
            result, vf_scene["grid"] = _load_generic_result(t)
        else:
            vf_scene["grid"], vf_scene["fields"] = _load_from_reader(vf_scene["reader"], t)
        vf_scene["bounds"] = vf_scene["grid"].GetBounds()
        deltas = [vf_scene["bounds"][i+1] - vf_scene["bounds"][i] for i in (0, 2, 4)]
        vf_scene["diag"] = sum(value**2 for value in deltas)**0.5 or 1.0
        for n in list(vf_objects):
            _vf_apply(n)
        if state.vf_selected in vf_objects:
            _vf_inspect(state.vf_selected)
        _viz_log(f"time -> {t}")
        _sync_info(vf_scene["grid"], time_value=t, path=vf_scene["information_path"])

    def vf_reset_camera():
        renderer.ResetCamera()

    def vf_shot(rel="figures/scene.png"):
        p = case_root / "D-post" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        render_window.Render()
        w2i = vtk.vtkWindowToImageFilter(); w2i.SetInput(render_window); w2i.Update()
        w = vtk.vtkPNGWriter(); w.SetInputConnection(w2i.GetOutputPort())
        w.SetFileName(str(p)); w.Write()
        _viz_log(f"screenshot -> D-post/{rel}")
        return str(p)

    def vf_list():
        return sorted(vf_objects.keys())

    @state.change("vf_pending_add")
    def _on_add(**_):
        kind = state.vf_pending_add
        if not kind:
            return
        state.vf_pending_add = ""
        creators = {"surface": vf_surface, "slice": vf_slice, "contour": vf_contour,
                    "stream": vf_stream, "glyph": vf_glyph, "isosurface": vf_isosurface,
                    "deform": vf_deform, "mode": vf_mode}
        if kind not in creators:
            return
        index = 1
        while f"VF_{kind}_{index}" in vf_objects:
            index += 1
        name = f"VF_{kind}_{index}"
        try:
            if vf_scene["reader"] is None:
                vf_load("")
            grid = vf_scene["grid"]
            if kind == "mode":
                fields = filters.field_names(grid, (1, 3))
                selected = next((field for field in fields if field.startswith("PODMode_")), None)
                if selected is None:
                    selected = next(iter(filters.field_names(grid, (3,))), None)
                if selected is None:
                    selected = next(iter(fields), None)
                vf_mode(name, selected)
            elif kind == "isosurface":
                fields = filters.field_names(grid, (1,))
                vf_isosurface(name, state.field if state.field in fields else next(iter(fields), None))
            else:
                creators[kind](name)
        except Exception as error:
            state.analysis_error = str(error) if isinstance(error, ValueError) else "无法构建此滤波器"
            _viz_log(f"{name} 构建失败: {type(error).__name__}")
            return
        state.analysis_error = ""
        _vf_select(name)
        _vf_render()

    facade_ns = {
        "VF_load": vf_load, "VF_slice": vf_slice, "VF_contour": vf_contour,
        "VF_stream": vf_stream, "VF_glyph": vf_glyph, "VF_surface": vf_surface,
        "VF_isosurface": vf_isosurface, "VF_deform": vf_deform, "VF_mode": vf_mode,
        "VF_color": vf_color, "VF_remove": vf_remove, "VF_time": vf_time,
        "VF_reset_camera": vf_reset_camera, "VF_shot": vf_shot, "VF_list": vf_list,
        "render": _vf_render,   # 脚本尾调用把结果推给浏览器
        "__file__": str(scenes_path),
        "VIBEFLOW_PARAVIEW_SCRIPT": str(scenes_path),
    }

    def _console_execute():
        import ast
        import inspect
        text = state.console_input.strip()
        if not text or len(text) > 500:
            state.console_output = "ERROR: 输入为空或超过 500 字符"
            return
        try:
            call = ast.parse(text, mode="eval").body
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
                raise ValueError("只允许单个 VF_* 函数调用")
            name = call.func.id
            if name != "render" and (not name.startswith("VF_") or name not in facade_ns):
                raise ValueError(f"函数不在控制台白名单: {name}")
            if any(isinstance(node, (ast.Attribute, ast.Subscript, ast.Lambda, ast.Await,
                                     ast.Yield, ast.YieldFrom, ast.GeneratorExp, ast.ListComp,
                                     ast.SetComp, ast.DictComp)) for node in ast.walk(call)):
                raise ValueError("控制台禁止属性、下标和推导式")
            args = [ast.literal_eval(arg) for arg in call.args]
            kwargs = {item.arg: ast.literal_eval(item.value) for item in call.keywords if item.arg}
            function = facade_ns[name]
            inspect.signature(function).bind(*args, **kwargs)
            if vf_scene["reader"] is None and name != "VF_load":
                vf_load("")
            result = function(*args, **kwargs)
            state.console_output = str(result)
            _vf_render()
        except Exception as error:
            state.console_output = f"ERROR: {error}"

    def _scene_failed():
        message = f"exec FAILED:\n{_tb.format_exc(limit=3)}"
        try:
            _viz_log(message)
        except OSError:
            print(message, flush=True)
        state.scenes_status = "执行失败（见 viz_executor.log）"

    def _run_scenes(raw):
        t0 = _time.time()
        try:
            import ast
            src = raw.decode('utf-8-sig')
            if not src.strip():
                return
            tree = ast.parse(src, filename=str(scenes_path))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Name) and node.id == "ViewSize") or (
                        isinstance(node, ast.Attribute) and node.attr == "ViewSize"):
                    raise ValueError("ViewSize 不允许修改远程视图尺寸")
            if vf_scene["reader"] is None:
                vf_load("")     # 首个脚本自动载入本会话算例
            facade_ns.update(__file__=str(scenes_path), VIBEFLOW_PARAVIEW_SCRIPT=str(scenes_path))
            exec(compile(tree, str(scenes_path), "exec"), facade_ns)
            _vf_render()
            _sync_vf_state()
            _viz_log(f"exec ok ({_time.time()-t0:.2f}s, {len(src)} 字符, "
                     f"对象 {sorted(vf_objects)})")
            state.scenes_status = f"已执行 · {len(vf_objects)} 个对象"
        except Exception:
            _scene_failed()

    def _scene_version():
        try:
            info = scenes_path.stat()
        except FileNotFoundError:
            return None
        return info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    async def _scenes_watcher():
        last = None
        read_failed = False
        try:
            last = _scene_version()
        except OSError:
            with state:
                _scene_failed()
            read_failed = True
        while True:
            await asyncio.sleep(0.25)
            try:
                revision = _scene_version()
                if revision is None:
                    last = None
                if revision == last:
                    continue
                await asyncio.sleep(0.5)
                if _scene_version() != revision:
                    continue
                raw = scenes_path.read_bytes()
                if _scene_version() != revision:
                    continue
            except FileNotFoundError:
                continue
            except OSError:
                if not read_failed:
                    with state:
                        _scene_failed()
                    read_failed = True
                continue
            last = revision
            read_failed = False
            with state:
                _run_scenes(raw)

    state.scenes_status = "监听中"
    state.vf_items = []
    state.vf_groups = []
    state.vf_selection_count = 0
    state.vf_menu_open = False
    state.vf_menu_point = [0, 0]
    state.vf_pipeline_storage = f"vf-pipeline-v1:{args.pipeline_scope}" if args.pipeline_scope else ""
    state.vf_saved_selection = None
    state.drawer = False   # 对象管线抽屉
    state.info_name = ""
    state.info_rows = []
    state.info_arrays = []
    state.paraview_busy = False
    state.paraview_message = ""
    state.paraview_rows = []
    state.paraview_arrays = []
    state.cmap_items = ["rainbow", "viridis", "coolwarm", "jet", "plasma", "inferno", "custom"]
    state.cmap_name = "rainbow"
    state.cmap_document = normalize_colormap(DEFAULT_CUSTOM_POINTS)
    state.cmap_points_text = colormap_json(state.cmap_document)
    state.cmap_spaces = list(COLOR_SPACES)
    state.cmap_space = "RGB"
    state.cmap_levels = 0
    state.cmap_preview = "none"
    state.cmap_status = ""
    state.cmap_invert = False
    state.cmap_log = False
    state.cmap_min = 0.0      # 0=自动（用数据范围）
    state.cmap_max = 0.0
    state.cmap_opacity = 1.0
    # Animation v1：时间播放 + 帧导出（关键帧轨道是 v2 明确缩减项）
    state.playing = False
    state.anim_fps = 2.0
    state.camera_track_obj = normalize_camera_track(DEFAULT_CAMERA_TRACK)
    state.camera_track_text = canonical_camera_track_json(DEFAULT_CAMERA_TRACK)
    state.camera_time = DEFAULT_CAMERA_TRACK["frames"][0]["t"]
    state.camera_playing = False
    state.camera_loop = False
    state.camera_status = ""
    state.saved_camera_view = None
    state.camera_bookmark_name = ""
    state.camera_bookmark_names = []
    state.camera_bookmark_selected = ""
    state.camera_bookmark_error = ""
    state.camera_can_undo = False
    state.camera_view_status = ""
    state.console_input = ""
    state.console_output = ""
    # Inspector：选中对象的参数编辑
    state.vf_selected = ""
    state.edit_field = ""
    state.analysis_error = ""
    state.edit_error = ""
    state.edit_scalar_items = []
    state.edit_vector_items = []
    state.edit_mode_items = []
    state.edit_vector = None
    state.edit_mode_field = None
    state.edit_imaginary = None
    state.edit_phase = 0
    state.edit_axis = "Z"
    state.edit_pos = 0.5
    state.edit_iso = 0.0
    state.edit_seeds = 40
    state.edit_scale = 1.0
    state.vf_pending_add = ""
    state.vf_apply_tick = 0

    @ctrl.add("on_server_ready")
    def _ready(**_):
        _load_camera_views()
        rebuild(reset_cam=True, grid=grid0)
        asyncio.get_running_loop().create_task(_scenes_watcher())
        if args.session_nonce:
            if server.port!=args.port:raise RuntimeError('Rendering server bound an unexpected port')
            import json
            import os
            from app.services.process_identity_native import observe
            message={"schema":"vibeflow.viz-ready.v1","nonce":args.session_nonce,
                     "profile_sha256":args.profile_sha256,"port":args.port,"process":observe(os.getpid())}
            print("\nVIBEFLOW_VIZ_READY_V1 "+json.dumps(message,sort_keys=True),flush=True)

    print(f"[viz-kernel] input={source_path} port={args.port} times={times} "
          f"fields={cell_fields} scalars={scalar_fields} scenes={scenes_path}", flush=True)
    server.start(port=args.port, host="127.0.0.1", open_browser=False,
                 show_connection_info=False, timeout=0)


if __name__ == "__main__":
    main()
