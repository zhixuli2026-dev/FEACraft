# 工作台全局主题

设置页的 **全局外观** 在 Dark 与 Light 之间切换。新浏览器会话默认显示 Dark。选择保存在该浏览器中，刷新后设置页和工作台恢复相同主题。根页面、Dockview、Monaco 编辑器、终端 SGR 和 CAD 画布使用一致的主题状态。Monaco 的资源由工作台发布包本地提供。[主题状态](../web/src/utils/theme.ts) · [设置页](../web/src/panels/SettingsPanel.tsx)

## 2026-11-19 实测

真实 Edge 在 SQLite 与文件系统上检查默认主题、设置切换、编辑器、终端、CAD 画布及再次打开后的值。Chromium 在 PostgreSQL 和 SeaweedFS 上通过同一流程，并先由实际工程入口生成 CadQuery 方体 `theme_box`。两个主题均打开实际网格画布和该对象。切换和刷新后，服务端读回的场景与原 JSON 相等，编辑源文件也逐字节保持。两组外部脚本、样式和字体请求、浏览器脚本错误、真实与模拟模型调用均为 0。[Edge 回执](../../tmp/browser-theme-e8mk8qlq/theme-browser.json) · [Chromium 回执](../../tmp/browser-theme-pinyhoqz/theme-browser.json) · [浅色场景](../../tmp/browser-theme-pinyhoqz/geometry-light.png) · [深色场景](../../tmp/browser-theme-pinyhoqz/geometry-dark.png)

本次验收复用现有主题实现。第一轮浏览器夹具点击时文件树尚在加载，随后按实际目录展开状态等待，完整流程通过。使用最终构建时可通过 `--channel` 选择浏览器，通过 `--postgres --s3` 复跑 PostgreSQL 与真实 S3。[第一次夹具记录](../../tmp/browser-theme-b9ga1z4n/theme-browser.json) · [复跑入口](../server/theme_browser_acceptance.py)
