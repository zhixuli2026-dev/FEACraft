# FF-A04 求解日志、残差和工程产物验收

2026-11-19 使用真实 Edge 和独立 SQLite／文件存储运行一维稳态导热算例。CadQuery 创建几何，WSL OpenFOAM v2406 的 `blockMesh` 和 `laplacianFoam` 生成网格和温度场，Python/VTK 提取温度，Pandoc 生成 HTML 和 DOCX。五项正式作业均发布成功，回执核验了进程停止。模型调用为 0，模拟调用为 0。[实测回执](../../tmp/solver-results-browser-g9962hh8/solver-results-result.json) · [OpenFOAM 日志](../../tmp/solver-results-browser-g9962hh8/openfoam.log)

- 作业页显示实际求解日志和末时刻 `t=1`。HTTP 解析结果与作业保存的结构化摘要一致。Edge 显示温度场的 initial/final 残差，切换线性／对数坐标后重新绘制，下载的 PNG 为 1280 × 520。[曲线](../../tmp/solver-results-browser-g9962hh8/solver-chart.png) · [PNG 原件](../../tmp/solver-results-browser-g9962hh8/residual_t1.png)
- 后处理体积平均温度为 `349.99999994412065 K`，与 `350 K` 的解析值相差约 `5.6 × 10⁻⁸ K`。后处理输出逐个核对发布清单的长度和 SHA-256，项目树可打开温度曲线 PNG 和 100 行采样 CSV。[温度曲线](../../tmp/solver-results-browser-g9962hh8/published-temperature-view.png)
- 工程报告内可阅读相同温度值。Edge 从历史列表选择正式版本并实际下载 HTML、DOCX，每份原件与相应发布清单的 SHA-256 一致。点击 HTML 正文中的 `temperature.png` 打开相应后处理文件。独立组织访问日志、后处理结果和报告原件均收到 HTTP 404。[报告截图](../../tmp/solver-results-browser-g9962hh8/published-docx-view.png)

修复前，报告从图件记录读 `path`，而后处理在 `artifacts` 中返回该次生成的文件名，导致图件链接为 `../None`。正文的旧相对路径也会从 `E-report/build/` 解析到错误目录。现在生成器只为清单中登记的图件文件生成链接，阅读器按算例根目录解析证据路径。将实际算例的 SQLite 与文件备份恢复到独立 API 后重新生成报告，沿用原后处理发布，新报告链接通过实际浏览器验证，旧报告原件仍可读且逐字节保持。[修复与新旧版本回执](../../tmp/solver-results-rebuilt-report-zto5qwlf/cold-report-rebuild.json) · [生成器](../server/app/services/report_builder.py)

本轮新端到端回执覆盖 Windows/Edge、SQLite/文件存储和一种实际 OpenFOAM 求解器。其它求解器日志格式、实时增量、长残差序列及断线恢复引用已有四种数据库与存储组合的实测。LaTeX、更多可视化格式及来源变更后重算沿用各自验收。[日志格式和摘要](job-solver-summary-acceptance.md) · [增量和恢复](residual-log-cursor-acceptance.md) · [长序列曲线](residual-chart-acceptance.md) · [LaTeX](latex-workflows.md) · [结果阅读器](result-readers.md) · [来源变更与重算](N6-05-case-panel-progress.md)

在项目根目录复跑，复用当天的前端构建目录及安装了 Playwright 和 Pillow 的本机 Python。

```powershell
& 'platform/server/.venv/Scripts/python.exe' -B -X utf8 `
  'platform/server/solver_results_browser_acceptance.py' `
  --dist 'tmp/codex-wsl-profile-20261119/web-dist' `
  --browser-python 'C:/ProgramData/anaconda3/python.exe' --channel msedge
```
