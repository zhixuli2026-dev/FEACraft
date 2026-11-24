"""为 PDF 每页追加底部居中的 page / count，不覆盖输入或已有输出。"""
from __future__ import annotations

from math import isfinite
from pathlib import Path, PurePosixPath

from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject


def safe_pdf_relative(value: str) -> str:
    path = value.strip().replace("\\", "/")
    if (not path or "\x00" in path or path.startswith("/")
            or (len(path) > 1 and path[1] == ":") or ".." in path.split("/")):
        raise ValueError("PDF 路径必须是算例内的相对路径")
    path = path.removeprefix("./").rstrip("/")
    if not path.lower().endswith(".pdf"):
        raise ValueError("输入和输出必须是 .pdf")
    return path


def numbered_pdf_path(input_path: str) -> str:
    source = PurePosixPath(input_path)
    return str(source.with_name(f"{source.stem}.numbered.pdf"))


def page_number_content(page_number: int, page_count: int, page, font_name: str) -> bytes:
    media = page['/MediaBox']
    crop = page.get('/CropBox', media).get_object()
    left, bottom = max(float(media[0]), float(crop[0])), max(float(media[1]), float(crop[1]))
    right, top = min(float(media[2]), float(crop[2])), min(float(media[3]), float(crop[3]))
    unit = float(page.user_unit)
    if not all(isfinite(value) for value in (left, bottom, right, top, unit)) or right <= left or top <= bottom or unit <= 0:
        raise ValueError("PDF 页面没有有效的可见范围")
    # Inverse of the viewer's clockwise page rotation. Keep the source page boxes and /Rotate.
    frames = {
        0: (1, 0, 0, 1, left, bottom),
        90: (0, 1, -1, 0, right, bottom),
        180: (-1, 0, 0, -1, right, top),
        270: (0, -1, 1, 0, left, top),
    }
    rotation = page.rotation % 360
    if rotation not in frames:
        raise ValueError("PDF 页面旋转必须为 90 度的整数倍")
    a, b, c, d, ox, oy = frames[rotation]
    width, height = right - left, top - bottom
    if rotation in (90, 270):
        width, height = height, width
    label = f"{page_number} / {page_count}"
    # Helvetica advance widths in thousandths of an em: digits 556, space and slash 278.
    label_width = (((len(label) - 3) * 556 + 3 * 278) * .009) / unit
    x = (width - label_width) / 2
    y = min(22 / unit, height / 2)
    basis = " ".join(f"{value / unit:.9f}".rstrip("0").rstrip(".") for value in (a, b, c, d))
    precision = 2 if unit == 1 else 8
    tx, ty = ox + a * x + c * y, oy + b * x + d * y
    return (
        f"q\nBT\n{font_name} 9 Tf\n0.35 g\n"
        f"{basis} {tx:.{precision}f} {ty:.{precision}f} Tm\n({label}) Tj\nET\nQ\n"
    ).encode("ascii")


def number_pdf(input_path: str | Path, output_path: str | Path) -> int:
    source = Path(input_path).resolve()
    output = Path(output_path).resolve()
    if source == output:
        raise ValueError("输出不能覆盖输入 PDF")
    if not source.is_file():
        raise FileNotFoundError(f"输入 PDF 不存在: {source}")
    reader = PdfReader(str(source))
    if reader.is_encrypted:
        raise ValueError("加密 PDF 暂不支持页码生成")
    page_count = len(reader.pages)
    if page_count <= 0:
        raise ValueError("输入 PDF 没有页面")
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
        NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
    })
    font_reference = writer._add_object(font)
    save_state = DecodedStreamObject()
    save_state.set_data(b"q\n")
    save_reference = writer._add_object(save_state)
    restore_state = DecodedStreamObject()
    restore_state.set_data(b"\nQ\n")
    restore_reference = writer._add_object(restore_state)
    for index, page in enumerate(writer.pages, 1):
        shared_resources = page.get("/Resources")
        resources = DictionaryObject(shared_resources.get_object() if shared_resources is not None else {})
        shared_fonts = resources.get("/Font")
        fonts = DictionaryObject(shared_fonts.get_object() if shared_fonts is not None else {})
        font_name = "/VFPageNumberFont"
        counter = 1
        while font_name in fonts:
            counter += 1
            font_name = f"/VFPageNumberFont{counter}"
        fonts[NameObject(font_name)] = font_reference
        resources[NameObject("/Font")] = fonts
        page[NameObject("/Resources")] = resources
        stream = DecodedStreamObject()
        stream.set_data(page_number_content(index, page_count, page, font_name))
        stream_reference = writer._add_object(stream)
        contents = page.get("/Contents")
        if contents is None:
            page[NameObject("/Contents")] = stream_reference
        else:
            resolved = contents.get_object()
            original = list(resolved) if isinstance(resolved, ArrayObject) else [contents]
            # Original graphics state (including transforms or clipping) stays confined to the original streams.
            page[NameObject("/Contents")] = ArrayObject([save_reference, *original, restore_reference, stream_reference])
    output.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        # xb 保证并发或重复请求也不会覆盖已有产物；失败只清理本次新建的部分文件。
        with output.open("xb") as stream:
            created = True
            writer.write(stream)
    except Exception:
        if created:
            output.unlink(missing_ok=True)
        raise
    return page_count
