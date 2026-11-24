"""可编辑 PPTX 垂直切片：文本、原生形状/表格/图片和模板保留。"""
from __future__ import annotations

import math
from pathlib import Path
import re

from PIL import Image
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE, PP_PLACEHOLDER
from pptx.enum.text import MSO_AUTO_SIZE, MSO_ANCHOR
from pptx.oxml.xmlchemy import OxmlElement
from pptx.util import Pt

# ST_ShapeType in spec order. Preset number is stable in the delivery catalog.
_PRESET_NAMES = (
    "line", "lineInv", "triangle", "rtTriangle", "rect",
    "diamond", "parallelogram", "trapezoid", "nonIsoscelesTrapezoid", "pentagon",
    "hexagon", "heptagon", "octagon", "decagon", "dodecagon",
    "star4", "star5", "star6", "star7", "star8",
    "star10", "star12", "star16", "star24", "star32",
    "roundRect", "round1Rect", "round2SameRect", "round2DiagRect", "snipRoundRect",
    "snip1Rect", "snip2SameRect", "snip2DiagRect", "plaque", "ellipse",
    "teardrop", "homePlate", "chevron", "pieWedge", "pie",
    "blockArc", "donut", "noSmoking", "rightArrow", "leftArrow",
    "upArrow", "downArrow", "stripedRightArrow", "notchedRightArrow", "bentUpArrow",
    "leftRightArrow", "upDownArrow", "leftUpArrow", "leftRightUpArrow", "quadArrow",
    "leftArrowCallout", "rightArrowCallout", "upArrowCallout", "downArrowCallout", "leftRightArrowCallout",
    "upDownArrowCallout", "quadArrowCallout", "bentArrow", "uturnArrow", "circularArrow",
    "leftCircularArrow", "leftRightCircularArrow", "curvedRightArrow", "curvedLeftArrow", "curvedUpArrow",
    "curvedDownArrow", "swooshArrow", "cube", "can", "lightningBolt",
    "heart", "sun", "moon", "smileyFace", "irregularSeal1",
    "irregularSeal2", "foldedCorner", "bevel", "frame", "halfFrame",
    "corner", "diagStripe", "chord", "arc", "leftBracket",
    "rightBracket", "leftBrace", "rightBrace", "bracketPair", "bracePair",
    "straightConnector1", "bentConnector2", "bentConnector3", "bentConnector4", "bentConnector5",
    "curvedConnector2", "curvedConnector3", "curvedConnector4", "curvedConnector5", "callout1",
    "callout2", "callout3", "accentCallout1", "accentCallout2", "accentCallout3",
    "borderCallout1", "borderCallout2", "borderCallout3", "accentBorderCallout1", "accentBorderCallout2",
    "accentBorderCallout3", "wedgeRectCallout", "wedgeRoundRectCallout", "wedgeEllipseCallout", "cloudCallout",
    "cloud", "ribbon", "ribbon2", "ellipseRibbon", "ellipseRibbon2",
    "leftRightRibbon", "verticalScroll", "horizontalScroll", "wave", "doubleWave",
    "plus", "flowChartProcess", "flowChartDecision", "flowChartInputOutput", "flowChartPredefinedProcess",
    "flowChartInternalStorage", "flowChartDocument", "flowChartMultidocument", "flowChartTerminator", "flowChartPreparation",
    "flowChartManualInput", "flowChartManualOperation", "flowChartConnector", "flowChartPunchedCard", "flowChartPunchedTape",
    "flowChartSummingJunction", "flowChartOr", "flowChartCollate", "flowChartSort", "flowChartExtract",
    "flowChartMerge", "flowChartOfflineStorage", "flowChartOnlineStorage", "flowChartMagneticTape", "flowChartMagneticDisk",
    "flowChartMagneticDrum", "flowChartDisplay", "flowChartDelay", "flowChartAlternateProcess", "flowChartOffpageConnector",
    "actionButtonBlank", "actionButtonHome", "actionButtonHelp", "actionButtonInformation", "actionButtonForwardNext",
    "actionButtonBackPrevious", "actionButtonEnd", "actionButtonBeginning", "actionButtonReturn", "actionButtonDocument",
    "actionButtonSound", "actionButtonMovie", "gear6", "gear9", "funnel",
    "mathPlus", "mathMinus", "mathMultiply", "mathDivide", "mathEqual",
    "mathNotEqual", "cornerTabs", "squareTabs", "plaqueTabs", "chartX",
    "chartStar", "chartPlus",
)
_PRESET_SET = frozenset(_PRESET_NAMES)
_PRESET_ALIASES = {"rectangle": "rect", "roundedRectangle": "roundRect"}
_CONNECTORS = frozenset(preset for preset in _PRESET_NAMES if "connector" in preset.lower()
                        and not preset.startswith("flowChart")) | {"line", "lineInv"}
_COLOR = re.compile(r"#[0-9a-fA-F]{6}\Z")


def ppt_preset_catalog() -> list[dict]:
    return [{"task_id": f"FF-E21.03.{index:03}", "preset": preset,
             "label": re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", preset).capitalize(),
             "kind": "connector" if preset in _CONNECTORS else "shape"}
            for index, preset in enumerate(_PRESET_NAMES, 1)]


def ppt_preset(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("PPT 图形预设必须是名称")
    preset = _PRESET_ALIASES.get(value, value)
    if preset not in _PRESET_SET:
        raise ValueError(f"PPT 图形预设未知: {value}")
    return preset


def ppt_geometry(shape) -> str | None:
    geometry = shape._element.xpath("./p:spPr/a:prstGeom")
    return geometry[0].get("prst") if geometry else None


def _add_preset(slide, preset, left, top, width, height):
    if preset in _CONNECTORS:
        shape = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, left, top, left + width, top + height)
    else:
        shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, left, top, width, height)
    shape._element.xpath("./p:spPr/a:prstGeom")[0].set("prst", preset)
    return shape


def _native_color(value) -> str:
    if not isinstance(value, str) or not _COLOR.fullmatch(value):
        raise ValueError("PPT 图形颜色必须为 #RRGGBB")
    return value.upper()


def _fraction(value) -> float:
    if type(value) not in (float, int) or not 0 <= value <= 1 or not math.isfinite(value):
        raise ValueError("PPT 图形坐标和尺寸必须介于 0 和 1")
    return float(value)


def _slide_bounds(item) -> dict:
    x, y = _fraction(item.get("x")), _fraction(item.get("y"))
    width, height = _fraction(item.get("width")), _fraction(item.get("height"))
    if width < 0.001 or height < 0.001 or x + width > 1.000000001 or y + height > 1.000000001:
        raise ValueError("PPT 对象必须位于页面范围内")
    return {"x": x, "y": y, "width": width, "height": height}


def reconstruction_bounds(item, width, height):
    return (round(item["x"] * width), round(item["y"] * height),
            round(item["width"] * width), round(item["height"] * height))


def _native_shapes(raw) -> list[dict]:
    if not isinstance(raw, list) or len(raw) > 40:
        raise ValueError("每页最多 40 个原生图形")
    shapes = []
    for item in raw:
        if not isinstance(item, dict) or set(item) - {"preset", "x", "y", "width", "height", "fill", "stroke"}:
            raise ValueError("PPT 原生图形包含未知字段")
        preset = ppt_preset(item.get("preset"))
        fill = item.get("fill", "#3B82F6" if preset not in _CONNECTORS else None)
        if fill is not None and preset in _CONNECTORS:
            raise ValueError("PPT 连接线使用描边颜色")
        shapes.append({"preset": preset, **_slide_bounds(item),
                       "fill": _native_color(fill) if fill is not None else None,
                       "stroke": _native_color(item.get("stroke", "#213A59"))})
    return shapes


def _reconstruction(value) -> dict:
    if not isinstance(value, dict) or set(value) != {"background", "aspect_ratio", "text_regions"}:
        raise ValueError("PPT 图片重建参数无效")
    ratio = value["aspect_ratio"]
    if type(ratio) not in (int, float) or not 0.25 <= ratio <= 4:
        raise ValueError("PPT 图片宽高比超出支持范围")
    regions = value["text_regions"]
    if not isinstance(regions, list) or len(regions) > 120:
        raise ValueError("每页最多重建 120 个文本框")
    result = []
    for item in regions:
        if not isinstance(item, dict) or set(item) != {"text", "color", "x", "y", "width", "height", "size"}:
            raise ValueError("PPT 重建文本框参数无效")
        size = _fraction(item["size"])
        if not 0.0025 <= size <= 0.75:
            raise ValueError("PPT 重建文本字号超出支持范围")
        result.append({**_slide_bounds(item), "text": _string(item["text"], "识别文字", 800, True),
                       "color": _native_color(item["color"]), "size": size})
    return {"background": _native_color(value["background"]), "aspect_ratio": float(ratio), "text_regions": result}


def ppt_slide_layout(presentation, selection):
    try:
        return presentation.slide_masters[selection["master"]].slide_layouts[selection["layout"]]
    except (IndexError, KeyError) as error:
        raise ValueError("PPT 模板中找不到指定母版或版式") from error


def _relative(value: str, suffixes: tuple[str, ...]) -> str:
    if not isinstance(value, str):
        raise ValueError("路径必须是字符串")
    path = value.strip().replace("\\", "/")
    if (not path or "\x00" in path or path.startswith("/")
            or (len(path) > 1 and path[1] == ":") or ".." in path.split("/")):
        raise ValueError("资源路径必须是算例内相对路径")
    path = path.removeprefix("./").rstrip("/")
    if not path.lower().endswith(suffixes):
        raise ValueError(f"路径扩展名不受支持: {path}")
    return path


def _string(value, field: str, limit: int, required=False) -> str:
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValueError(f"{field} 必须是长度不超过 {limit} 的{'非空' if required else ''}字符串")
    return value


def validate_ppt_document(document: dict) -> dict:
    if not isinstance(document, dict):
        raise ValueError("PPT 请求必须是对象")
    unknown = set(document) - {"output", "title", "template", "slides"}
    if unknown:
        raise ValueError(f"未知 PPT 字段: {sorted(unknown)}")
    output = _relative(document.get("output", "E-report/build/vibeflow.pptx"), (".pptx",))
    title = _string(document.get("title", "VibeFlow"), "文档标题", 200, True)
    template = document.get("template")
    template = _relative(template, (".pptx",)) if template else None
    if template == output:
        raise ValueError("输出不能覆盖模板")
    raw_slides = document.get("slides")
    if not isinstance(raw_slides, list) or not 1 <= len(raw_slides) <= 50:
        raise ValueError("slides 必须包含 1–50 页")
    slides = []
    for index, raw in enumerate(raw_slides, 1):
        if not isinstance(raw, dict) or set(raw) - {"title", "bullets", "text", "shape", "table", "image",
                                                  "native_shapes", "beautify", "template_layout", "notes", "reconstruction"}:
            raise ValueError(f"第 {index} 页结构或字段无效")
        title_value = _string(raw.get("title"), f"第 {index} 页标题", 200, True)
        raw_bullets = raw.get("bullets", [])
        if not isinstance(raw_bullets, list) or len(raw_bullets) > 20:
            raise ValueError("每页 bullets 至多 20 项")
        bullets = [_string(item, "bullet", 500) for item in raw_bullets]
        text = _string(raw.get("text", ""), "正文", 5000)
        shape = raw.get("shape", "roundedRectangle")
        ppt_preset(shape)
        raw_table = raw.get("table")
        table = None
        if raw_table is not None:
            if not isinstance(raw_table, dict) or set(raw_table) - {"headers", "rows"}:
                raise ValueError("table 只能包含 headers/rows")
            headers = raw_table.get("headers")
            rows = raw_table.get("rows", [])
            if not isinstance(headers, list) or not 1 <= len(headers) <= 10 or not isinstance(rows, list) or len(rows) > 50:
                raise ValueError("表格需 1–10 列且至多 50 行")
            headers = [_string(item, "表头", 200, True) for item in headers]
            normalized_rows = []
            for row in rows:
                if not isinstance(row, list) or len(row) != len(headers):
                    raise ValueError("表格必须是矩形")
                if any(not isinstance(cell, (str, int, float, bool, type(None))) for cell in row):
                    raise ValueError("表格单元格必须是标量")
                normalized_rows.append(["" if cell is None else str(cell) for cell in row])
            table = {"headers": headers, "rows": normalized_rows}
        raw_image = raw.get("image")
        image = None
        if raw_image is not None:
            if not isinstance(raw_image, dict) or set(raw_image) != {"path"}:
                raise ValueError("image 只能包含 path")
            image = {"path": _relative(raw_image["path"], (".png", ".jpg", ".jpeg"))}
        spec_slide = {"title": title_value, "bullets": bullets, "text": text,
                      "shape": shape, "table": table, "image": image}
        if "native_shapes" in raw:
            spec_slide["native_shapes"] = _native_shapes(raw["native_shapes"])
        if "beautify" in raw:
            if type(raw["beautify"]) is not bool:
                raise ValueError("PPT 美化选项必须为布尔值")
            spec_slide["beautify"] = raw["beautify"]
        if "template_layout" in raw:
            selection = raw["template_layout"]
            if (not template or not isinstance(selection, dict) or set(selection) != {"master", "layout"}
                    or any(type(value) is not int or not 0 <= value <= 255 for value in selection.values())):
                raise ValueError("PPT 版式需要同页模板和有效的母版/版式编号")
            spec_slide["template_layout"] = selection
        if "notes" in raw:
            spec_slide["notes"] = _string(raw["notes"], "演讲者备注", 10000)
        if "reconstruction" in raw:
            if template or any(raw.get(option) for option in ("beautify", "bullets", "text", "image", "table")):
                raise ValueError("PPT 图片重建页需要独立版式")
            reconstruction = _reconstruction(raw["reconstruction"])
            if not reconstruction["text_regions"] and not spec_slide.get("native_shapes"):
                raise ValueError("PPT 图片重建页需要至少一个可编辑对象")
            spec_slide["reconstruction"] = reconstruction
        slides.append(spec_slide)
    return {"output": output, "title": title, "template": template, "slides": slides}


def _beautify_slide(slide, title, body, table_frame):
    canvas = RGBColor.from_string("F5F8FC")
    ink = RGBColor.from_string("152941")
    muted = RGBColor.from_string("355067")
    primary = RGBColor.from_string("154A71")
    white = RGBColor.from_string("FFFFFF")
    slide.background.fill.solid()
    slide.background.fill.fore_color.rgb = canvas

    title.text_frame.word_wrap = True
    title.text_frame.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
    heading = title.text_frame.paragraphs[0]
    heading.font.name = "Aptos Display"
    heading.font.size = Pt(32)
    heading.font.color.rgb = ink
    if body is not None:
        frame = body.text_frame
        frame.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
        for paragraph in frame.paragraphs:
            paragraph.font.name = "Aptos"
            paragraph.font.color.rgb = muted
            paragraph.line_spacing = 1.1
            paragraph.space_after = Pt(4)
    if table_frame is not None:
        table = table_frame.table
        table.first_row = False
        table.horz_banding = False
        for index, row in enumerate(table.rows):
            for cell in row.cells:
                cell.fill.solid()
                cell.fill.fore_color.rgb = primary if index == 0 else white if index % 2 else canvas
                cell.vertical_anchor = MSO_ANCHOR.MIDDLE
                cell.margin_top = cell.margin_bottom = Pt(2)
                for paragraph in cell.text_frame.paragraphs:
                    paragraph.font.name = "Aptos"
                    paragraph.font.color.rgb = white if index == 0 else ink


def _render_native_shapes(slide, width, height, items):
    for index, item in enumerate(items, 1):
        native = _add_preset(slide, item["preset"], *reconstruction_bounds(item, width, height))
        native.name = f"VibeFlowPreset{index}"
        native.line.color.rgb = RGBColor.from_string(item["stroke"][1:])
        native.line.width = Pt(1.5)
        if item["preset"] not in _CONNECTORS:
            if item["fill"] is None:
                native.fill.background()
            else:
                native.fill.solid()
                native.fill.fore_color.rgb = RGBColor.from_string(item["fill"][1:])


def _render_reconstruction(slide, width, height, details, shapes):
    slide.background.fill.solid()
    slide.background.fill.fore_color.rgb = RGBColor.from_string(details["background"][1:])
    _render_native_shapes(slide, width, height, shapes)
    for index, item in enumerate(details["text_regions"], 1):
        box = slide.shapes.add_textbox(*reconstruction_bounds(item, width, height))
        box.name = f"VibeFlowOCR{index}"
        frame = box.text_frame
        frame.margin_left = frame.margin_right = frame.margin_top = frame.margin_bottom = 0
        frame.word_wrap = False
        frame.text = item["text"]
        for paragraph in frame.paragraphs:
            paragraph.font.name = "Aptos"
            paragraph.font.size = Pt(item["size"] * (height / 12700))
            paragraph.font.color.rgb = RGBColor.from_string(item["color"][1:])


def build_pptx(case_root: str | Path, document: dict) -> dict:
    root = Path(case_root).resolve()
    spec = validate_ppt_document(document)

    def resolve(relative: str) -> Path:
        target = (root / relative).resolve()
        if root != target and root not in target.parents:
            raise ValueError("资源路径越出算例根目录")
        return target

    template_path = resolve(spec["template"]) if spec["template"] else None
    output = resolve(spec["output"])
    if template_path == output:
        raise ValueError("输出不能覆盖模板")
    prs = Presentation(str(template_path)) if template_path else Presentation()
    prs.core_properties.title = spec["title"]
    if spec["slides"][0].get("reconstruction"):
        prs.slide_width = round(prs.slide_height * spec["slides"][0]["reconstruction"]["aspect_ratio"])
    blank = next((layout for layout in prs.slide_layouts if layout.name.lower() == "blank"),
                 prs.slide_layouts[min(6, len(prs.slide_layouts) - 1)])
    width, height = int(prs.slide_width), int(prs.slide_height)
    margin = int(width * 0.05)
    for spec_slide in spec["slides"]:
        layout = ppt_slide_layout(prs, spec_slide["template_layout"]) if "template_layout" in spec_slide else blank
        slide = prs.slides.add_slide(layout)
        if spec_slide.get("notes"):
            slide.notes_slide.notes_text_frame.text = spec_slide["notes"]
        if "reconstruction" in spec_slide:
            _render_reconstruction(slide, width, height, spec_slide["reconstruction"], spec_slide.get("native_shapes", []))
            continue
        title = slide.shapes.title if "template_layout" in spec_slide else None
        if title is None:
            title = slide.shapes.add_textbox(margin, int(height * 0.05), int(width * 0.9), int(height * 0.1))
        title.name = "VibeFlowTitle"
        title.text_frame.text = spec_slide["title"]
        if not title.is_placeholder:
            title.text_frame.paragraphs[0].font.size = Pt(28)
            title.text_frame.paragraphs[0].font.bold = True
        preset = ppt_preset(spec_slide["shape"])
        accent = _add_preset(slide, preset, margin, int(height * 0.17), int(width * 0.025), int(height * 0.035))
        accent.name = "VibeFlowAccent"
        if preset in _CONNECTORS:
            accent.line.color.rgb = RGBColor(59, 130, 246)
        else:
            accent.fill.solid()
            accent.fill.fore_color.rgb = RGBColor(59, 130, 246)
            accent.line.fill.background()
        has_image = spec_slide["image"] is not None
        content_width = int(width * (0.47 if has_image else 0.9))
        lines = list(spec_slide["bullets"])
        if spec_slide["text"]:
            lines.append(spec_slide["text"])
        body = None
        if lines:
            if "template_layout" in spec_slide and not (has_image or spec_slide["table"]):
                body = next((shape for shape in slide.placeholders if shape.placeholder_format.type in
                             {PP_PLACEHOLDER.BODY, PP_PLACEHOLDER.OBJECT, PP_PLACEHOLDER.SUBTITLE}), None)
            if body is None:
                body = slide.shapes.add_textbox(margin, int(height * 0.23), content_width, int(height * 0.22))
            body.name = "VibeFlowBody"
            frame = body.text_frame
            frame.word_wrap = True
            frame.clear()
            for index, line in enumerate(lines):
                paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
                bullet = index < len(spec_slide["bullets"])
                paragraph.text = f"• {line}" if bullet and not body.is_placeholder else line
                if not body.is_placeholder:
                    paragraph.font.size = Pt(18)
                else:
                    marker = OxmlElement("a:buChar" if bullet else "a:buNone")
                    if bullet:
                        marker.set("char", "•")
                    paragraph._p.get_or_add_pPr().append(marker)
        table_frame = None
        if spec_slide["table"]:
            table_spec = spec_slide["table"]
            rows = [table_spec["headers"], *table_spec["rows"]]
            table_frame = slide.shapes.add_table(len(rows), len(table_spec["headers"]), margin, int(height * 0.49),
                                                 content_width, int(height * 0.4))
            table_frame.name = "VibeFlowTable"
            for row_index, row in enumerate(rows):
                for column_index, value in enumerate(row):
                    cell = table_frame.table.cell(row_index, column_index)
                    cell.text = value
                    for paragraph in cell.text_frame.paragraphs:
                        for run in paragraph.runs:
                            run.font.size = Pt(12)
                            run.font.bold = row_index == 0
        if has_image:
            image_path = resolve(spec_slide["image"]["path"])
            if not image_path.is_file():
                raise FileNotFoundError(f"图片不存在: {image_path}")
            max_width = int(width * (0.36 if lines or spec_slide["table"] else 0.7))
            max_height = int(height * 0.58)
            with Image.open(image_path) as image:
                image_width, image_height = image.size
            scale = min(max_width / image_width, max_height / image_height)
            picture_width = max(1, int(image_width * scale))
            picture_height = max(1, int(image_height * scale))
            left = int(width * 0.58) if lines or spec_slide["table"] else int((width - picture_width) / 2)
            slide.shapes.add_picture(str(image_path), left, int(height * 0.24),
                                     width=picture_width, height=picture_height)
        if spec_slide.get("beautify"):
            _beautify_slide(slide, title, body, table_frame)
        _render_native_shapes(slide, width, height, spec_slide.get("native_shapes", []))
    output.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with output.open("xb") as stream:
            created = True
            prs.save(stream)
    except Exception:
        if created:
            output.unlink(missing_ok=True)
        raise
    return {"output": spec["output"], "slides": len(prs.slides)}
