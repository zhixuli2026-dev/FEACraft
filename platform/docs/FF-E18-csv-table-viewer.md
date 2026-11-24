# FF-E18 CSV 与 TSV 表格查看

工作台从项目文件树打开 CSV 或 TSV。解析使用工作线程，表格按滚动位置渲染可见行及邻近行。跳转控件使用当前筛选和排序后的行号，左侧行号显示源数据位置。点击列标题可按数值或文本排序，也可取消排序。行详情保留完整文字，包括引号和换行。解析或列数不齐有独立提示，额外的列可见，缺少值的单元格留空。[表格实现](../web/src/panels/TablePanel.tsx) · [解析工作线程](../web/src/utils/delimitedWorker.ts)

搜索可查全部列或指定列。勾选可跨筛选保留，表头复选框选择当前全部匹配。用户可导出所选行或整个筛选结果，导出采用当前排序及带 BOM 的 UTF-8 CSV。可能被表格软件当作公式执行的文本加引号前缀，科学计数法和带符号的普通数字保留原样。源文件编辑沿用已有编辑器入口。[文本读取路由](../server/app/routers/files.py)

## 实际验收

通过正式文件 API 上传 2,730,888 B、60,000 行、8 列的 CSV。真实 Edge 的旧版默认挂载 2,000 行，滚动范围内没有第 60,000 行。最终版初次挂载 24 行，跳转可将末行完整显示在表格视口。相同验证数据中，旧版从点击文件树到表头出现为 4.875 秒，最终轮为 1.094 秒。这是本机两次实际观测。[旧版对照](../../tmp/csv-viewer-browser-advd4018/csv-viewer-result.json) · [末行截图](../../tmp/csv-viewer-browser-q79vlu6z/csv-last-row.png)

浏览器验证数值升序、降序、取消排序，指定列筛选、无匹配状态、跨筛选选择和两类导出。两行所选和 30,000 行筛选结果由真实浏览器下载，随后逐行检查 UTF-8 CSV。完整行详情和下载保留带逗号、引号和换行的原值。另用实际未闭合引号及列数不齐文件验证解析提示，用空文件验证空状态。[行详情](../../tmp/csv-viewer-browser-q79vlu6z/csv-row-detail.png) · [所选行导出](../../tmp/csv-viewer-browser-q79vlu6z/selected.csv) · [筛选导出](../../tmp/csv-viewer-browser-q79vlu6z/filtered.csv)

早期验收发现，文件 API 将 TSV 返回为二进制，浏览器提示读取失败。加入文本后缀后，正常接口返回 TSV 文本，真实浏览器保留引号内的制表符。另一次请求对照先让文件树完成原有编辑器读取，再在独立隔离服务延迟查看器的第二次读。切换到 TSV 后释放原 CSV，浏览器核对真实 HTTP 200 及完整请求结束，TSV 仍显示。外部账号返回 404，无凭据返回 401，隔离 API、监听端口均已退出。真实和模拟模型调用均为 0。[TSV 旧故障](../../tmp/csv-viewer-browser-hk09r515/csv-failure.png) · [窄窗口复验](../../tmp/csv-viewer-browser-q79vlu6z/csv-small-workbench.png) · [最终回执](../../tmp/csv-viewer-browser-q79vlu6z/csv-viewer-result.json) · [验收入口](../server/csv_viewer_browser_acceptance.py)
