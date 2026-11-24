# FF-A21 普通二进制下载

2026-11-19。原审计要求普通二进制按钮通过 Bearer 读取受保护的 `/raw`，按原文件名保存并释放 Blob URL。编辑器此前设置了下载状态，但二进制视图没有显示它。现在按钮读取期间显示状态并阻止重复提交，失败后显示原因并恢复重试。关闭文件时会终止未完成的请求，浏览器开始保存后显示确认。现有文件 API 和签名方式沿用原实现。[原审计](feature-gap-audit.md) · [编辑器](../web/src/panels/EditorPanel.tsx)

- 实际 Edge 打开生产构建，使用键盘下载 `网格 数据-é.bin`，读到 4,194,307 字节，文件名和 SHA-256 均与上传原件相符。等待真实请求期间，按钮禁用且重复触发只发出一个请求。两次浏览器保存后都回收了各自的 Blob URL。另一笔实际请求在关闭文件时以 `ERR_ABORTED` 结束。[最新浏览器和 API 回执](../../tmp/a21-binary-download-d8tv4dmx/binary-download-result.json) · [成功界面](../../tmp/a21-binary-download-d8tv4dmx/binary-download-success.png)
- 未登录与未共享用户请求均被拒绝，获授权的只读用户可下载且仍无法上传。撤销共享后，同一编辑器点击收到真实 404 并显示失败。恢复共享后，原按钮可直接重试，文件名及字节与首次下载相同。`HEAD`、单段 `Range` 和缺失文件响应也经真实 API 验证。隔离 SQLite 与文件系统 API 已退出，全程模型调用为 0。[失败界面](../../tmp/a21-binary-download-d8tv4dmx/binary-download-error.png) · [原验收回执](../../tmp/a21-binary-download-tn7rcojc/binary-download-result.json)
- 对象存储保留原有的 S3 签名 URL 读取及凭据策略。真实 PostgreSQL 与 SeaweedFS 的签名版本和 Chromium 保存沿用已有验收记录，本轮 Edge 验收覆盖 Bearer `/raw` 路径。[S3 兼容证据](FF-A11-editor-conflict-acceptance.md#验收记录) · [S3 下载回执](../../tmp/live-cli-startup-bunzeqrt/file-download.json)

复验使用服务端虚拟环境、已构建前端及装有 Playwright 的 Python。使用以下入口即可运行隔离验收。[复验脚本](../server/binary_download_browser_acceptance.py) · [生产构建](../../tmp/a21-binary-build-20261119.log) · [界面检测](../../tmp/a21-impeccable-detect-20261119.json)

```powershell
& 'platform/server/.venv/Scripts/python.exe' -B -X utf8 `
  'platform/server/binary_download_browser_acceptance.py' `
  --dist 'tmp/a21-binary-dist-20261119' `
  --browser-python 'C:/ProgramData/anaconda3/python.exe' --channel msedge
```
