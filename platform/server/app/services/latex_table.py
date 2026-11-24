"""安全的 LaTeX tabular 生成：矩形校验、转义、booktabs 与边框。"""
from __future__ import annotations

_ESCAPE = {
    "\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "&": r"\&", "%": r"\%",
    "$": r"\$", "#": r"\#", "_": r"\_", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
}


def latex_escape(value) -> str:
    # A plain tabular cell cannot contain a TeX paragraph break from pasted CSV.
    text = str(value).replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    return "".join(_ESCAPE.get(char, char) for char in text)


def latex_table(headers: list[str], rows: list[list[object]], booktabs: bool = True,
                borders: bool = False, align: str | None = None) -> str:
    if not isinstance(headers, list) or not headers or len(headers) > 20:
        raise ValueError("表头须为 1–20 列")
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError("数据行须为至多 100 行")
    columns = len(headers)
    for row in rows:
        if not isinstance(row, list) or len(row) != columns:
            raise ValueError("每行必须与表头列数一致")
    if align is None:
        align = "l" * columns
    if not isinstance(align, str) or len(align) != columns or any(char not in "lcr" for char in align):
        raise ValueError("align 必须逐列由 l/c/r 组成")
    spec = f"|{ '|'.join(align) }|" if borders else align
    lines = [f"\\begin{{tabular}}{{{spec}}}"]
    if booktabs:
        lines.append("\\toprule")
    elif borders:
        lines.append("\\hline")
    lines.append(" & ".join(latex_escape(item) for item in headers) + " \\\\")
    if booktabs:
        lines.append("\\midrule")
    elif borders:
        lines.append("\\hline")
    for row in rows:
        lines.append(" & ".join(latex_escape(item) for item in row) + " \\\\")
        if borders and not booktabs:
            lines.append("\\hline")
    if booktabs:
        lines.append("\\bottomrule")
    lines.append("\\end{tabular}")
    return "\n".join(lines)
