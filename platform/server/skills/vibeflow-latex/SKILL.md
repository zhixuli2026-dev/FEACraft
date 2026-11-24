---
name: vibeflow-latex
description: LaTeX 写作与编译契约：报告论文级文档的 main.tex 结构、公式/引用/图表规范、latexmk 编译配方（WSL TeX Live）。当需要 LaTeX 精排报告/论文时使用。
---

# LaTeX 契约（自有重写版）

**支持范围**　注册编译在配有 TeX Live 的 WSL 或 Linux 执行。XeLaTeX、LuaLaTeX、pdfLaTeX、biber、PDF 和 SyncTeX 有实际产物验收。

## 什么时候用

pandoc（E-report/report.md → HTML/DOCX）够大多数内部报告用；需要期刊级排版、精确引用、复杂公式编号时才走 LaTeX。

## 项目布局（E-report/latex/ 下）

```
main.tex            # 主文档（\input 各 sections）
sections/*.tex      # 分章节
refs.bib            # 参考文献库
figures/ tables/    # 图（PDF 矢量优先）/表
build/              # 编译产物（latexmk -outdir）
```

## 写作规范

- 中文优先 `xeCJK`+XeLaTeX 或 LuaLaTeX 配方；英文可 pdflatex。**BibTeX/Biber 不混用**（本版统一 biber+`biblatex`）。
- 公式：`equation` 环境+`\label{eq:...}`；引用用 `\eqref{eq:...}`；文献 `\cite{key}`（key 来自 refs.bib）。
- 图：`\includegraphics` 引用 `figures/` 下 PDF/PNG，caption 写清该图回答什么问题。
- 表：优先 booktabs 三线表（`\toprule/\midrule/\bottomrule`），不用竖线网格。
- 物理量单位用 `siunitx`（`\SI{1.2}{\meter\per\second}` 风格）。

## 编译（本平台）

- 在算例面板选择主文件和配方，或提交 `POST /api/v1/cases/{id}/latex:build`。注册作业在独立源快照运行 `latexmk`，参数包含 `-no-shell-escape -norc`。
- 排错只读**第一个 `!` 错误**（后续级联错误大多是误报）；改完重跑。
- 从 LaTeX 发布记录打开 PDF、日志与对应的源版本。

## 与 Markdown 报告的关系

`E-report/report.md` 是快速通道；`E-report/latex/` 是精排通道。两者章节结构一致（见 vibeflow-report 技能），图片共用 `D-post/figures/`（相对路径引用）。
