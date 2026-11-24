# FF-H05 残差曲线增量续读验收

打开正在运行的作业曲线后，浏览器每次等上一请求结束再发起下一次读取。后续请求携带完整行偏移与 SHA-256 前缀指纹，已有曲线只追加新记录。选择其他作业、切回日志或关闭面板会取消当前读取。网络断开时，界面显示错误并保留已读曲线，恢复后重新使用原游标。

作业日志接口沿用作业和算例的查看权限。执行进程已在当前服务登记时可读取其实际日志片段，因此 S3 模式能在上传完成前显示该进程的进展。可见的本地文件和 S3 对象分别按文件身份和存储版本读取。服务暂时看不到日志时显示等待状态。跨主机的独立实时日志传输继续由作业日志与部署矩阵跟踪。

增量读取按原始字节核验整个已消费前缀，只消费新增完整行。发生截断或重写时以 409 告知浏览器，浏览器清除旧数据并从头解析。分段收到常规 `Time` 时，曲线摘要以常规时刻为准，即使前后读到了 `TimeStep`。作业结束后通过原解析接口读取一次最终日志，包含末尾无换行的 `End`，历史日志也沿用此接口。前缀指纹校验需要读取已消费字节。作业摘要的独立持久化和超长诊断处理见 FF-H06。[作业摘要验收](job-solver-summary-acceptance.md)

## Chromium 实测

| 数据库与存储 | 浏览器请求与终态摘要 | 真实模型工具回读 |
|---|---|---|
| SQLite 与文件系统 | [实测](../../tmp/live-cli-startup-nh5bxkh0/residual-cursor.json) | [模型回执](../../tmp/live-cli-startup-nh5bxkh0/live-result.json) |
| PostgreSQL 与文件系统 | [实测](../../tmp/live-cli-startup-xvl8eq7f/residual-cursor.json) | [模型回执](../../tmp/live-cli-startup-xvl8eq7f/live-result.json) |
| SQLite 与 S3 | [实测](../../tmp/live-cli-startup-otzh_nnh/residual-cursor.json) | [模型回执](../../tmp/live-cli-startup-otzh_nnh/live-result.json) |
| PostgreSQL 与 S3 | [实测](../../tmp/live-cli-startup-4nj99dnu/residual-cursor.json) | [模型回执](../../tmp/live-cli-startup-4nj99dnu/live-result.json) |

四组各运行一个真实 Windows 作业，在独立 Chromium 中核对半行续接、实际带指纹的 HTTP 请求、断线重试、线性轴保留、八种兼容标记中的四种标记绘制、重写后清除旧场、终态补全与停轮询。无效游标返回 400，其他账户返回 404。生产构建在桌面和 420 px 窄屏查看。此前的服务端重启复读覆盖八种兼容标记，同一游标三个并发请求一致，截断和重写均返回 409。四个新增场景合计 8 次真实 `deepseek-flash` HTTP 200，模拟调用为 0。[桌面增量曲线](../../tmp/live-cli-startup-xvl8eq7f/incremental-chart-desktop.png) · [窄屏终态](../../tmp/live-cli-startup-nh5bxkh0/incremental-chart-mobile.png)

同一前端构建另复跑 SQLite 与文件系统、PostgreSQL 与 S3 上的原曲线验收，共 6 个实际作业。PNG 导出、长曲线峰谷、线性与对数轴、历史 final-only 返回、迟到应答和刷新恢复均通过。两组回归产生 4 次真实 `deepseek-flash` HTTP 200。[SQLite 文件复验](../../tmp/live-cli-startup-8hx4uwyl/residual-chart.json) · [PostgreSQL S3 复验](../../tmp/live-cli-startup-gwzwpdtc/residual-chart.json)

使用服务器虚拟环境运行 `live_llm_residual_cursor_acceptance.py --source-profile-id 26`，可追加 `--postgres` 与 `--s3`。设置 `VIBEFLOW_TEST_WEB_DIST` 指向实际构建，`VIBEFLOW_TEST_BROWSER_PYTHON` 指向已安装 Playwright 的 Python。现有偏移与指纹字段保持兼容。前端和 API 可随应用版本一起回滚，无数据库迁移。[验收入口](../server/live_llm_residual_cursor_acceptance.py) · [浏览器断言](../server/residual_cursor_browser.py)
