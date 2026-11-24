# FF-E09 LaTeX 表格生成

工作台的 LaTeX 表格生成器支持普通、booktabs 和边框样式，并按列采用 `l`、`c` 或 `r`。表头和数据可粘贴逗号分隔文本，引号内的逗号保留为同一个单元格。服务逐字转义 LaTeX 特殊字符，把单元格内部换行折成空格，同时保留列数和对齐校验。使用 booktabs 样式需要在文档导言区加载该包。[表格生成服务](../server/app/services/latex_table.py) · [booktabs 文档](https://mirrors.ctan.org/macros/latex/contrib/booktabs/booktabs.pdf)

预览随输入、对齐和样式绑定，修改后插入会重新生成当前表格。插入沿用工作台选中的 LaTeX 主文件。在真正的文档结束标记前插入时保留原有内容和注释。随后通过原有文件版本校验写入，并保存逐字节一致的前序备份。已打开的干净编辑器读取新版本并定位表格。有未保存编辑时先完成正常保存。远端文件发生并发修改时保留表格输入和备份，可重新读取主文件后重试。[工作台表格入口](../web/src/panels/CasePanel.tsx)

`/latex:table` 保持既有编辑权限要求。工作区查看者可通过正常入口读取已有 `.tex`，表格生成和插入控件禁用。[报告路由](../server/app/routers/reports.py)

## 复验

在 `platform/server` 运行已登记的 `latex_table_browser_acceptance.py`，`--dist` 指向当前工作台生产构建，`--browser-python` 指向安装 Playwright 的 Python。测试使用真实 Edge 和 SQLite/文件系统隔离 API，从工作台提交实际 XeLaTeX 作业，再通过发布读取 PDF 和冻结源码，并用实际 Poppler 提取 PDF 文本。[验收入口](../server/latex_table_browser_acceptance.py)

早期夹具通过详细读取取得 `workspace_id` 后才完成共享设置。后续一轮在工作台能力探测期间正常保存返回 409，Monaco 草稿仍在，服务端保持原文件。再一轮保存实际返回 200，但夹具在工作台显示保存结束前点击插入，草稿保护提示要求先完成保存。相关回执保留，正式夹具等待正常保存结束后才试写表格。[首轮退出](../../tmp/e09-latex-table-8njixyej/cleanup-independent.json) · [存储占用时保留草稿](../../tmp/e09-latex-table-s56incxt/table-browser-failure.png) · [保存状态与退出](../../tmp/e09-latex-table-phi2rap1/cleanup-independent.json)

## 2026-11-19 正式验收

真实 Edge 验证短行与错误对齐返回 422。改动输入后旧预览消失，实际点击插入采用当前输入。主文件有未保存草稿时，表格操作保留本地稿和服务器原件。完成普通 Monaco 保存后，第二次真实条件写制造了同一文件的版本变化，浏览器原请求收到 `409 file_conflict`。表格预览、三份相应的前序备份和并发修改均随后核验。重试成功，且两个表格均位于真正的文档结束标记之前。[冲突恢复](../../tmp/e09-latex-table-1y44aetd/table-conflict.png) · [插入后 Monaco](../../tmp/e09-latex-table-1y44aetd/inserted-monaco.png)

工作台提交的注册作业 `#1` 用 XeTeX 生成一页、8,569 字节的 PDF，退出码为 0。通过隔离 API 读取的冻结 `.tex` SHA-256 与浏览器保存收据一致。Poppler 从真实 PDF 读回 `A&B`、中文列名、`Quoted,cell`、`a_b`、`%$#\{}~^`、合并后的跨行单元格和边框样式独立行。[真实 PDF](../../tmp/e09-latex-table-1y44aetd/table.pdf) · [提取文本](../../tmp/e09-latex-table-1y44aetd/table-rendered.txt) · [正式结果](../../tmp/e09-latex-table-1y44aetd/latex-table-result.json)

外部账号的表格 API 返回 404，已授权只读账号读源文件返回 200，生成和写入均返回 404，Edge 对应控件禁用。独立复核确认 API、作业 worker、三个 Windows Job Object 及其 WSL 进程退出，数据库无活跃原生请求。全程零模型调用。阶段开始及完成后 C 盘分别可用 22.13 GiB 和 22.11 GiB，Docker 构建缓存均为 0 B。[只读状态](../../tmp/e09-latex-table-1y44aetd/table-readonly.png) · [独立退出复核](../../tmp/e09-latex-table-1y44aetd/cleanup-independent.json) · [前端检测](../../tmp/e09-table-impeccable-20261119.json)
