# FF-E03 三种 LaTeX 配方与 PDF

2026-11-19。原合同覆盖 XeLaTeX、LuaLaTeX、pdfLaTeX 真实编译，以及 biber 产生的 `.bbl` 和可追踪日志。临时目录、超时、禁 shell escape 和并发控制由 E04 记录。既有正式编译器固定使用 `latexmk`，按主文件保存不可变版本、源码和原生停止回执。[原审计](feature-gap-audit.md) · [注册编译协议](latex-workflows.md)

LaTeX 面板过去每次点击直接提交，刷新后没有已受理作业的跟踪。本次复用后端幂等键。浏览器按用户和算例在当前标签页保存原主文件、配方和键。提交结果不明时由用户确认原请求，已知作业在刷新后只查询状态。诊断仍可定位源文件，成功后从正常文件树打开和下载该 PDF。[面板实现](../web/src/panels/CasePanel.tsx) · [幂等接口](../server/app/routers/reports.py)

实际编译由 `latexmk` 组织对应引擎和所需的 BibLaTeX 后端，biber 提供 UTF-8 参考文献处理。[latexmk 官方包说明](https://ctan.org/pkg/latexmk) · [biber 官方包说明](https://ctan.org/pkg/biber)

## 真实环境验收

XeTeX（TeX Live 2023）与 biber 2.19 产出了一页 PDF、1,667 字节的 `main.bbl` 和 21,304 字节的编译日志。三份文件与不可变发布的长度和 SHA-256 一致。`pdftotext` 读出了中文正文、文中引注和参考文献。Edge 先丢弃服务器已受理的 HTTP 响应，再用同一幂等键确认原作业。刷新时作业仍在运行，浏览器只查询状态，作业列表始终只有一条。[浏览器回执](../../tmp/e03-real-latex-fml33749/latex-browser.json) · [发布独立读回](../../tmp/e03-real-latex-fml33749/xelatex-publication-independent.json) · [进程检查](../../tmp/e03-real-latex-fml33749/cleanup-independent.json)

首次浏览器脚本对 Playwright 拦截期间的事件数做了过严断言。修正后失回执和刷新步骤通过。该次执行的 LuaLaTeX 阶段超过脚本原有的 175 秒页面观察窗口，仍在原生编译时由夹具取消，退出后独立确认 Windows 与 WSL 原生 scope 已清理。后续专用阶段扩大了测试观察窗口以覆盖注册编译的实际预算。[首次结果](../../tmp/e03-real-latex-epqgk9ao/latex-browser.json) · [观察截止后的浏览器截图](../../tmp/e03-real-latex-fml33749/latex-browser-failure.png)

一次 LuaLaTeX 复验在并行的工作台 Python 和 WSL 自检期间遇到 `LaTeX toolchain probe did not finish`，当次创建了 0 个编译作业。五个探测进程的监督范围已停止，并独立复核了 Windows 与 WSL 进程身份。退回空档后，同一非特权 TeX 探测在 0.62 秒内返回真实工具链信息。[探测超时及清理证据](../../tmp/e03-real-latex-84yhgnx6/native-preparation-timeout.json)

空档复验实际发布 LuaLaTeX PDF，故意破坏的 pdfLaTeX 源文件在第 4 行产生 `Undefined control sequence`。失败保留了此前的 LuaLaTeX 发布。Edge 显示了正确源路径，点击时却重复拼接主文件目录而返回 404。诊断现在直接使用注册编译端给出的算例相对路径，继续拒绝绝对路径与越界路径。[Lua PDF 独立读回](../../tmp/e03-real-latex-jhr0rjot/lua-publication-independent.json) · [错误回执](../../tmp/e03-real-latex-jhr0rjot/latex-browser.json) · [故障截图](../../tmp/e03-real-latex-jhr0rjot/latex-browser-failure.png) · [路径修复](../web/src/utils/latexDiagnostics.ts)

最终回放复制了这次实际发布的 4,023 字节 LuaHBTeX PDF、文件历史和 SQLite。真实失败作业经只读恢复，未新增编译请求。Edge 点击诊断后在 Monaco 打开 `E-report/latex/main.tex` 第 4 行，以原 SHA-256 作为保存前置条件完成修正。两次常规环境自检返回 HTTP 200 后点击编译，真实 pdfTeX 作业发布一页 14,537 字节 PDF。浏览器从文件树打开 PDF.js 预览并下载 `main.pdf`，下载哈希与发布清单一致。之前一次夹具等 HTTP 回执超时后取消了新作业，该作业宿主进程、Windows 监督范围和 WSL 进程均已独立复核退出。[诊断跳转](../../tmp/e03-real-latex-1178qw8t/latex-diagnostic-open.png) · [成品和下载](../../tmp/e03-real-latex-1178qw8t/latex-pdf-editor.png) · [前次回执超时后的进程检查](../../tmp/e03-real-latex-0ra5ume0/admission-stop-independent.json)

独立账号在被授权前无法读文件或编译。获得工作区 viewer 权限后能读旧 PDF，写入编译仍被拒绝。真实 API 冷重启后，两份 PDF 都按发布 ID 读回原字节，旧 LuaLaTeX 源文件仍可读取。两个 API 进程均已退出，独立复核 3 个作业宿主、8 个作业子范围及 11 个请求子范围的原进程身份已消失。验收使用 D 盘隔离的 SQLite 和文件存储，模型调用为 0。C 盘开始为 22.20 GiB，结束为 22.21 GiB。Docker 构建缓存始终为 0，11 个运行容器及 39 个活动卷继续运行。[最终回执](../../tmp/e03-real-latex-1178qw8t/latex-recipes-result.json) · [独立退出检查](../../tmp/e03-real-latex-1178qw8t/cleanup-independent.json)

## 复验入口

先通过工作台正常的 TypeScript 检查和 Vite 构建，再从 `platform/server` 启动登记过的专用脚本。需要已装好 TeX Live 的 `vibeflow-linux` WSL、带 Playwright 的 Python 和 Microsoft Edge。测试在 D 盘创建独立文件根和 SQLite，使用实际的正常工作台 HTTP 路由、非特权 WSL 执行和 PDF.js。模型调用为 0。[验收脚本](../server/latex_recipes_browser_acceptance.py)

```powershell
& .venv/Scripts/python.exe -B -X utf8 latex_recipes_browser_acceptance.py `
  --dist D:/vibeflow_reverse/tmp/e03-latex-diagnostics-dist-20261119 `
  --browser-python C:/ProgramData/anaconda3/python.exe --channel msedge
```

已有 XeLaTeX 与 biber 阳性时，附加 `--phase lua-pdf` 可单独复验剩余配方、错误恢复、权限和冷重启。

已有完成并退出的真实 LuaLaTeX 和 pdfLaTeX 错误回执时，可使用 `--phase lua-pdf --resume-pdf-from <证据目录>`。夹具在新隔离目录中复制文件根和 SQLite，校验旧 PDF 哈希，按原编译键和作业编号恢复诊断。正式源文件修正走普通浏览器保存与版本检查，后续编译通过正式 API 真正启动。
