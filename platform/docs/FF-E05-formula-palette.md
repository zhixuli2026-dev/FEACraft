# FF-E05 公式调色板与编辑器插入

2026-11-19，合入引用面板后在最终前端构建上再次通过真实 Edge。六类符号、KaTeX、Monaco 撤销、只读保护和保存后独立读回均通过。[合并复验](../../tmp/e05-formula-palette-14cvf_0x/formula-palette-result.json)

2026-11-19。原合同覆盖六类符号、KaTeX 预览、行内 `$…$`、展示 `$$…$$`、带可选标签的 `equation` 环境，以及插入 Monaco。在线文献和公式 OCR 分别按自身交付项验收。[原审计 E05](feature-gap-audit.md) · [公式调色板](../web/src/panels/FormulaPanel.tsx)

预览使用本地 KaTeX，并生成可访问的 MathML。KaTeX 的异常消息可能包含输入片段，页面把它作为纯文本展示。编辑器身份与当前打开路径匹配且编辑执行成功后才清空公式。未打开文件、处于 Markdown 预览或只有只读权限时，页面保留公式，并提示如何继续。标签会拒绝破坏 `\label{…}` 边界的字符。插入或替换当前选区可独立撤销。[KaTeX 错误处理](https://katex.org/docs/error) · [Monaco 插入实现](../web/src/shell/editorRef.ts)

## 实际验收

Edge 点选了六类符号中的代表项，KaTeX 0.19.0 同时输出可见公式和 MathML，覆盖分数与矩阵。错误作为纯文本显示，纠正后恢复预览。Monaco 0.57.0 从原选区分别写入行内、展示、有标签和无标签的公式环境，并按一次 Ctrl+Z 撤销单次插入。无文件、Markdown 预览、非法标签和只读工作区均保留草稿。实际 UI 中只读账号能预览并打开共享文件，普通 API 写入返回 404。未获授权前的读取也返回 404。[实际工作台](../../tmp/e05-formula-palette-ptqgchg2/formula-editor.png) · [浏览器回执](../../tmp/e05-formula-palette-ptqgchg2/formula-browser.json)

首次真实保存返回账户结算中的 HTTP 409，编辑器保留全部公式，服务端仍为原文件。复验经正常保存按钮按原 SHA-256 执行一次有界重试，服务端写入成功。实际浏览器刷新后，Monaco、授权文件读取接口与原件流读取三处内容一致，最终 SHA-256 为 `6feecfc611ea2a92e65eb679cddfd0a6f42a16489a5e644a23fdd47d8cc326d5`。另一份 Markdown 保持原字节。隔离 API 已退出，独立确认六个已登记后台自检进程的原身份已消失。模型调用为 0。构建前后 C 盘均为 22.20 GiB，Docker 构建缓存为 0。[首次 409](../../tmp/e05-formula-palette-z3dxzu96/formula-browser-failure.png) · [完整结果](../../tmp/e05-formula-palette-ptqgchg2/formula-palette-result.json) · [独立退出证据](../../tmp/e05-formula-palette-ptqgchg2/cleanup-independent.json)

## 复验入口

从 `platform/server` 启动已登记的专用入口。使用实际生产构建、Edge、D 盘独立 SQLite 和文件存储。[浏览器验收](../server/formula_palette_browser_acceptance.py)

```powershell
& .venv/Scripts/python.exe -B -X utf8 formula_palette_browser_acceptance.py `
  --dist D:/vibeflow_reverse/tmp/e05-formula-palette-dist-20261119 `
  --browser-python C:/ProgramData/anaconda3/python.exe --channel msedge
```
