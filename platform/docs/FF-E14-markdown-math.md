# FF-E14 Markdown 公式预览

原版 `VibeFlowStudio.exe` 的 Markdown 预览配置使用随程序分发的 MathJax 3.2.2 TeX/MathML 组件。UTF-16 配置包含四种行内及独立公式分隔符、转义、无分隔符的数学环境，以及显式启用的 AMS、`cases`、`textmacros`、`noerrors` 和 `noundefined`。预览跳过 `pre` 和 `code`。用原版实际配置和工程文档样例定义本次兼容矩阵。[原版配置读回](../../tmp/ff-e14-original-mathjax-settings.json) · [MathJax 配置说明](https://docs.mathjax.org/en/v3.2/input/tex/extensions/) · [TeX 与 HTML](https://docs.mathjax.org/en/v3.2/input/tex/html.html)

| 表达式或行为 | 现有路径与通过的真实 Edge 验证 |
| --- | --- |
| `$…$`、`\(…\)`、`$$…$$`、`\[…\]` | 普通文件继续用 KaTeX，积分、根号和希腊字母四项通过；未下载兼容组件 |
| 裸 `align`、显式 `\tag`、`\label` 与 `\eqref` | 本地 MathJax 在同一文档中给质量守恒式排版，前文引用显示 `(M42)` |
| `cases` 扩展的 `numcases` | 两种条件分支生成真实 SVG 括号和表格行；`empheq` 提供括号所需的依赖 |
| `textmacros` 与局部宏 | `\text{…\enspace\textbf{…}}` 显示混排，`\newcommand` 后续公式读取同文件单位宏 |
| 原始 MathML | 分数 7/13 生成 SVG 与辅助 MathML；普通 Markdown 表格仍可读 |
| 错误与隔离 | 未定义命令可回读，代码围栏、行内代码与转义货币按原文显示。换文件后原标签不会进入新文档 |

后备组件按需从同源构建资源加载。输入与输出均经过 HTML 消毒，兼容引擎使用固定的本地扩展及安全配置。实际恶意标签、事件属性、JavaScript 链接和公式中的风险超链接均未执行。兼容模块失败时仍可查看正文和原式。SVG 附带辅助 MathML。[预览组件](../web/src/panels/MarkdownPreview.tsx) · [兼容配置](../web/src/utils/markdownMathCompatibility.ts) · [安全选项](https://docs.mathjax.org/en/v3.2/options/safe.html)

## 真实验收

旧构建通过标准公式和表格对照，但 `numcases` 仍显示为源码。修复版首轮实际截图显示 `\empheqlbrace` 泄漏为文本，补齐依赖后，最终截图与辅助 MathML 均显示括号和分支。最终回合核对原 Markdown 字节、预览重开、文档切换、外部账号 404、匿名 401 和隔离 API 实际退出。Edge 页面错误、外网请求、真实和模拟模型调用均为 0。[旧版对照](../../tmp/markdown-math-browser-1sg7i763/markdown-math-result.json) · [首轮截图](../../tmp/markdown-math-browser-w79xs84k/markdown-compatibility.png) · [最终截图](../../tmp/markdown-math-browser-tkm9r4b4/markdown-compatibility.png) · [最终回执](../../tmp/markdown-math-browser-tkm9r4b4/markdown-math-result.json) · [验收入口](../server/markdown_math_browser_acceptance.py)
