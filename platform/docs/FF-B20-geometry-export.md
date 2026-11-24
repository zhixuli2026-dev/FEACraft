# FF-B20 几何面板一键导出 STEP/STL

「导出 STEP/STL」提交真实 CadQuery 导出作业，面板显示作业号。导出与几何预览分别跟踪，各自展示终态。面板在同一作业尚未结束时限制重复提交。切换算例后，旧算例的迟到响应不会改变当前面板。

导出成功后刷新文件树。若文件树暂时无法刷新，仍显示导出成功，可从「已发布的 STEP / STL」打开结果。失败时已发布的版本仍可下载，修复脚本后可再次导出。已选发布保持固定，通过「读取最新发布」切换版本。下载按版本核验原件的大小与 SHA-256。[几何面板](../web/src/panels/GeoPanel.tsx) · [发布下载组件](../web/src/panels/GeometryDownloads.tsx)

独立验收使用真实 Edge、CadQuery、本机文件存储与 SQLite。[验收入口](../server/geometry_export_browser_acceptance.py)

- 一键导出作业 `#2` 退出码为 0。Edge 显示完成回执，重新请求并展开 `A-geo/exports`，显示 STEP 和 STL。两个浏览器下载的文件头、大小和 SHA-256 均通过检查。
- 实际 CadQuery 导出钩子故意失败，作业 `#4` 退出码为 1。同一期间提交的预览作业 `#5` 正常退出，几何面板继续显示导出失败。此前选定的 STEP/STL 再次实际下载，与原下载字节摘要一致。[失败与旧发布画面](../../tmp/geometry-export-browser-lllr1m40/failed-export-retained.png)
- 修复脚本后，一键导出作业 `#7` 正常退出。文件树出现新两个文件，下载区仍保持原选择。点击「读取最新发布」后，两种新原件均可下载，摘要均与旧版本不同。跨账号访问版本清单与原件均返回 404。模型调用为 0。[新版本画面](../../tmp/geometry-export-browser-lllr1m40/retry-export.png) · [最终验收回执](../../tmp/geometry-export-browser-lllr1m40/geometry-export-result.json)
