# FF-B14 CAD 截图与回传

几何预览「视角与截图」中的「截图」将当前 CAD 画布保存为 `<算例名>-cad.png`。浏览器按画布实际像素尺寸编码 PNG。

原查看器收到 `{type: "screenshot", filename}` 后调用 `viewer.getImage`，再把 PNG data URL 放入 `{command: "screenshot", text: {filename, data}}`。原资源中的发送适配器写入控制台。它的 WebSocket 负责接收场景替换和清空。Web 平台通过同页消息提供对应回执，保留请求中的文件名。截图只读取当前账号与算例已完成绘制的画布。尚未就绪时同页回执的 `data` 为 `null`，按钮提示等待模型显示并重试。跨窗口或跨来源消息没有截图回执。[原截图入口](../../extracted/resources/cadviewer/index.html#L1782) · [Web 实现](../web/src/panels/GeoPanel.tsx)

验收用独立文件存储与 SQLite、真实 CadQuery 和 Edge。[验收入口](../server/geometry_screenshot_browser_acceptance.py)

真实 Edge 验证了原同页事件与 JSON 消息，两种入口回传相同像素并保留中文文件名。按钮触发实际浏览器下载，文件名为 `CAD screenshot-cad.png`。回传与下载均为 `555 × 398` 像素，和当时的画布尺寸一致。画面可见带孔板，隐藏该对象时对应像素发生变化。[同页回传 PNG](../../tmp/geometry-screenshot-browser-46iatez8/message-capture.png) · [浏览器下载 PNG](../../tmp/geometry-screenshot-browser-46iatez8/downloaded-cad.png)

另一来源的 iframe 截图消息抵达后没有回执。将真实画布宽度短暂设为 0 触发了按钮错误提示，恢复尺寸后同一算例可再次截图。切换算例的同一调用栈与随后仍为空的算例都返回 `data: null`，切回原算例后重新获得 PNG。服务器场景前后完全一致，另一个账号读取时收到 404。模型调用为 0。[恢复画面](../../tmp/geometry-screenshot-browser-46iatez8/restored-cad.png) · [通过回执](../../tmp/geometry-screenshot-browser-46iatez8/geometry-screenshot-result.json)
