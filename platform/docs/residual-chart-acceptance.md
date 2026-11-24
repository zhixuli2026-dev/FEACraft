# 求解残差曲线与日志摘要

作业面板在核对所选作业和尝试后读取日志。切换作业时会终止旧浏览器请求，迟到的返回不会替换当前曲线。读取失败可在原作业下重新解析，刷新工作台后仍可选中之前打开的作业。旧版只提供 final 残差序列时，仍可查看该序列。

对数轴用指数范围定位已取对数的样本，负指数可以正常绘制。线性轴能显示零残差，原迭代位置保持不变。每个场继续使用 initial 虚线和 final 实线。长序列按画布列保留首尾与峰谷。窄屏界面将作业列表放在曲线上方，420 px 视口可用接近全宽查看并导出。图例、坐标和实际曲线一起导出为 PNG。

## 验收记录

| 数据库与存储 | 实际 Windows 作业和 Chromium | 真实模型回执 |
|---|---|---|
| SQLite 与文件系统 | [验收](../../tmp/live-cli-startup-gm05ukkf/residual-chart.json) | [DeepSeek](../../tmp/live-cli-startup-gm05ukkf/live-result.json) |
| PostgreSQL 与文件系统 | [验收](../../tmp/live-cli-startup-43w0zouq/residual-chart.json) | [DeepSeek](../../tmp/live-cli-startup-43w0zouq/live-result.json) |
| SQLite 与 S3 | [验收](../../tmp/live-cli-startup-qwbjl83e/residual-chart.json) | [DeepSeek](../../tmp/live-cli-startup-qwbjl83e/live-result.json) |
| PostgreSQL 与 S3 | [验收](../../tmp/live-cli-startup-oqrp51au/residual-chart.json) | [DeepSeek](../../tmp/live-cli-startup-oqrp51au/live-result.json) |

- 每组实际执行三个隔离 Windows 子进程，生成可复算的日志。普通时刻格式显示末时刻 0.2、Courant max 0.5、用时 2.0 秒与正常结束。第二时刻格式显示 0.7、一个警告、一个致命错误及缺少 End 标志，作业记录同时保留实际失败状态。
- 每组浏览器核验 SVG 所有位置为有限数，并比较两种轴模式的实际像素位置。零残差在线性图显示为基线上的点。5,000 条长序列绘制 1,114 个代表点，保留首尾迭代位置及两个独立峰谷。实际下载 PNG 尺寸为 1280×520，检查了曲线区域的像素。[PNG 下载结果](../../tmp/live-cli-startup-oqrp51au/residual-download.png) · [窄屏曲线](../../tmp/live-cli-startup-oqrp51au/residual-narrow.png)
- 四组均验证断网重试、切换作业时迟到应答、旧版 final-only 应答、重新加载后的作业选择及跨账户访问拒绝。断网和迟到由浏览器控制，正常和旧版格式测试中的源文件均由实际作业写出。服务端日志按块解析及指纹校验另有四组实测。[日志接口证据](solver-log-stream-parsing.md)

SQLite 与 S3 并行首轮有一个 Windows 作业在写入 `attempt.finalization_prepared` 后未按时提交终态，180 秒后由测试请求取消。该次未进入曲线验证。独立复跑完成三个作业、浏览器验证和模型回读。工作流提交迟滞的原因仍在跨部署故障验收中跟踪。[失败记录](../../tmp/live-cli-startup-6jm0_bxl/live-result.json) · [状态证据](../../tmp/residual-s3-job-finalization-timeout-20261119.json)

四组最终验收共 8 次真实 `deepseek-flash` HTTP 200，前两组试跑另有 4 次。模型读取存于隔离算例的验收摘要。实际坐标、图片像素、权限及作业状态由 API 和浏览器独立核对。

使用服务器虚拟环境运行 `live_llm_residual_chart_acceptance.py --source-profile-id 26`，可追加 `--postgres` 及 `--s3`。设置 `VIBEFLOW_TEST_WEB_DIST` 为生产构建目录，`VIBEFLOW_TEST_BROWSER_PYTHON` 为装有 Playwright 和 Pillow 的 Python。[复验入口](../server/live_llm_residual_chart_acceptance.py) · [浏览器硬断言](../server/residual_chart_browser.py)
