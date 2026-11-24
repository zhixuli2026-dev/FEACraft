# FF-E10 LaTeX 诊断与 PDF 回跳

结构化诊断通过 LaTeX 作业日志提供错误、警告和算例相对源码行号。`GET /latex/errors` 使用与作业详情一致的算例读权限，校验作业属于请求中的算例。工作区查看者可以读取诊断和源码，编译及文件写入仍需要编辑权限。[诊断和反向查询入口](../server/app/routers/reports.py) · [编译器与失败日志原验收](FF-E03-latex-recipes.md)

PDF 查看器双击时先用 PDF.js 将显示点还原为 PDF 坐标，包含缩放和平面旋转的逆变换。`POST /synctex:query` 使用所选 PDF 原件的 MediaBox 左上角换算坐标，再交给真实 `synctex edit`。裁切只改变所显示的范围，源码映射仍采用编译页的坐标。接口默认接收既有的页面左上坐标，查看器发送 `coordinate_space: "pdf"` 时才进行坐标换算。[TeX Live 坐标说明](https://tug.tug.org/pipermail/tex-live-commits/2017-October/004366.html) · [PDF 查看器](../web/src/panels/ViewerPanel.tsx) · [已选原件与换算](../server/app/services/synctex_queries.py)

普通文件回跳时绑定读取时的文件版本，版本已变时返回 409。普通 PDF 缺少同名 `.synctex.gz` 时提示重新编译。历史 PDF 回跳固定同一个发布 ID，展示该次发布保存的只读源码与行号。[历史选择与只读源码](latex-history-selection.md)

真实 XeLaTeX 取证中，编译原件的 `(260, 131)` 命中 `main.tex` 第 5 行。裁切框从左侧移入 40 bp、从顶部移入 90 bp 后，原实现会提交 `(220, 41)`，同一个真实 sidecar 返回了第 7 行。回归用相同的 XeLaTeX PDF 和 sidecar，另外在文档元数据中应用裁切及 90 度旋转。

## 复验

在 `platform/server` 用已登记的 `latex_synctex_browser_acceptance.py` 运行。`--dist` 指向本地生产构建，`--history` 指向已退出的 E03 LuaLaTeX/pdflatex 两发布和失败作业副本，`--xe` 指向 E03 实际 XeLaTeX 发布，`--browser-python` 指向安装 Playwright 的 Python。夹具复制原有隔离数据库与文件，使用正常工作台交互及实际 WSL SyncTeX，无模型调用。[验收入口](../server/latex_synctex_browser_acceptance.py)

首轮 Edge 已从真实 `Undefined control sequence` 跳到 Monaco 第 4 行，并在读取早期 LuaLaTeX 发布时发出带正确发布 ID 的查询。当时同一宿主正在运行其他原生进程，两次工作台自检与查询重叠，SyncTeX 返回 504。独立复核确认查询范围与 API 均已退出，私有快照目录为空。复验等真实工作台能力检查结束后双击。[首轮 API 日志](../../tmp/e10-synctex-wxgu90u5/api.log) · [首轮退出复核](../../tmp/e10-synctex-wxgu90u5/cleanup-independent.json)

第二轮旧发布回跳已由原生查询返回 200，冻结源码第 3 行与工作区当前源码分别读回。后续夹具按 100% 默认缩放计算，工作台从 120% 开始，裁切用例在点击前停止。原回执保留，夹具随后修正为通过普通缩放按钮到达约 60%。[第二轮浏览器结果](../../tmp/e10-synctex-d9_dctx0/browser.json) · [历史只读源码截图](../../tmp/e10-synctex-d9_dctx0/historical-source.png) · [第二轮退出复核](../../tmp/e10-synctex-d9_dctx0/cleanup-independent.json)

第三轮浏览器和坐标检查通过。克隆的组织已使用两席，重复邀请触发了 429。后续夹具登录原先完成邀请并获授权的查看者，同时保留新外部账号的 404 检查。第三轮的四个实际 SyncTeX 查询、API 端口和私有装配目录均已复核关闭。[第三轮原记录](../../tmp/e10-synctex-ui46rnc_/synctex-result.json) · [第三轮退出复核](../../tmp/e10-synctex-ui46rnc_/cleanup-independent.json)

## 2026-11-19 正式验收

真实 Edge 与隔离 SQLite/文件系统通过完整流程，入口退出码为 0。现有真实 pdflatex 失败诊断和浏览器定位均为 `main.tex:4`。早期 LuaLaTeX 发布的双击返回只读 `main.tex:3`，同时确认现在的工作区源码内容已变。打开最新发布恢复读取新 PDF。[错误与 Monaco 截图](../../tmp/e10-synctex-zyfsou5e/native-failure-monaco.png) · [历史发布截图](../../tmp/e10-synctex-zyfsou5e/historical-source.png)

带裁切和 90 度旋转的 XeLaTeX PDF 在 60% 缩放下，真实鼠标双击提交绝对 PDF 点约 `(260, 710.83)`。缺少同名 sidecar 时返回 409 并显示重新编译提示。经普通上传补回原 sidecar 后，同一点返回 200，Monaco 跳至 `main.tex:5`。旧版页面坐标接口仍返回第 5 行，错误的裁切原点输入实际返回第 7 行。版本指纹失效返回 409，越界页和坐标返回 422。[缺少 sidecar](../../tmp/e10-synctex-zyfsou5e/missing-sidecar.png) · [修复后定位](../../tmp/e10-synctex-zyfsou5e/rotated-crop-monaco.png)

外部账号查询和诊断均返回 404。已有工作区查看者查询和诊断均返回 200，访问另一算例的作业诊断或修改源文件均返回 404。共完成 5 次实际原生 SyncTeX 查询，独立复核确认对应 Windows Job Object、Windows 和 WSL 进程、服务端口及临时快照均已关闭或清理。全程零模型调用。阶段前后 C 盘可用空间为 22.15 GiB 和 22.14 GiB，Docker 构建缓存均为 0 B。[正式结果](../../tmp/e10-synctex-zyfsou5e/synctex-result.json) · [独立退出复核](../../tmp/e10-synctex-zyfsou5e/cleanup-independent.json) · [前端检测](../../tmp/e10-synctex-impeccable-20261119.json)
