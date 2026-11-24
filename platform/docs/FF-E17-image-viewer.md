# FF-E17 图片查看

2026-11-19，图片查看通过实际 Edge、生产前端构建和隔离 Windows API 验收。覆盖 Bearer Blob 读取、90° 旋转、缩放及复位、拖拽、两种全屏路径、读取换档和文件修复后的重试。真实和模拟模型调用均为 0。[最终回执](../../tmp/image-viewer-browser-22vzjssj/image-viewer-result.json)

## 复现与修复

旧构建加载 1920×210 的 PNG 后，旋转包围盒高 647 像素，而画布高 323 像素。上传 PNG 扩展名的损坏文件后，原始接口返回 HTTP 200，浏览器解码尺寸为零且页面没有错误提示。最终构建按旋转方向交换画布宽高约束，同一张图旋转后高 323 像素，整图留在画布内。查看器区分网络读取与浏览器解码，提供原位重新读取，并在文件、算例或账号变化时取消旧请求和撤销旧 Blob URL。[旧构建对照](../../tmp/image-viewer-browser-_5nfwo8b/image-viewer-result.json) · [旋转截图](../../tmp/image-viewer-browser-22vzjssj/image-rotated.png) · [解码提示截图](../../tmp/image-viewer-browser-22vzjssj/image-decode-error.png)

首轮修复验证发现，浏览器拒绝原生全屏后，固定位置的查看器仍受 Dockview 容器限制，从窗口顶部 34 像素处开始。最终版将降级查看器挂载到文档根层，真实浏览器测得它完整覆盖 1480×830 的窗口。降级全屏中的滚轮缩放、Escape 退出和缩放状态保留均通过，原生全屏也验证了进入与退出。[首轮修复回执](../../tmp/image-viewer-browser-8x5s6pj_/browser-result.json) · [原生全屏截图](../../tmp/image-viewer-browser-22vzjssj/image-native-fullscreen.png) · [降级全屏截图](../../tmp/image-viewer-browser-22vzjssj/image-fallback-fullscreen.png)

## 文件与权限验收

- 使用实际上传写入隔离算例。损坏图片显示解码错误后，通过带预期 SHA-256 的真实写入换成 420×160 的 PNG，点击「重新读取」后恢复显示。
- 切换文件时浏览器报告旧请求为 `net::ERR_ABORTED`，随后打开的图片正确显示。真实按钮和滚轮分别触发缩放，实际指针移动改变图片位置，双击复位恢复 100% 与原位置。
- 无权限账号读取同一文件返回 404，无凭据返回 401；隔离 API 已退出。验证范围为 Windows 上的 Edge 桌面浏览器。[验收入口](../server/image_viewer_browser_acceptance.py)
