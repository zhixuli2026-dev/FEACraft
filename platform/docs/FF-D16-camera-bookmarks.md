# FF-D16 截图和相机书签

在工作台的可视化抽屉中输入书签名，再选择「保存书签」。点击「应用书签」恢复选定镜头。书签保存相机位置、焦点、上方向、投影模式、缩放和视角。书签随算例文件保存，重新打开同一结果文件仍可读取。不同结果文件各自保有书签。[相机实现](../server/app/services/viz_kernel.py)

「撤回视角」回到应用书签、恢复临时视角或点击预设之前的镜头。同名书签会显示提示并保留原记录。读取到损坏的书签时仍可应用其他书签，修复文件后可用「重读书签」更新列表。

「保留截图」在 `D-post/figures/` 创建独立 PNG。「截图」继续写到 `D-post/figures/view-preset.png`。从文件树可以回读两种截图。书签使用 `D-post/camera-views/` 下按结果文件区分的目录。

本项涉及同一个工作台中独立打开的多个可视化会话。完整 ParaView 多视口工作区在 D01 规划，CAD 四宫格在 B09 跟踪。[完整交付计划](full-function-delivery-plan.md)

## 实际验收

2026-11-19 使用隔离 SQLite 和本地算例存储，在 Edge 157 同时打开两个独立 Windows 原生 VTK 会话。工作台的「读取 ParaView 源数据」让独立 pvpython 5.11.2 读取了正在渲染的 VTP。正式验收退出码 0，浏览器异常、未处理的 Promise 错误和模型请求均为 0。[完整回执](../../tmp/viz-camera-d16-3y77oooz/camera-bookmarks-result.json) · [验收入口](../server/viz_camera_bookmarks_browser_acceptance.py)

- 键盘提交成功命名，空名显示字段内错误，大小写不同的同名提交保留已存 JSON。+X 与等轴测的实际 PNG 像素不同。应用 +X、撤回至等轴测、关闭重开后恢复等轴测、修复坏记录后恢复 +X，分别重现原 467×434 PNG 的完整 SHA-256。[工作台](../../tmp/viz-camera-d16-3y77oooz/camera-bookmarks-workbench.png) · [+X](../../tmp/viz-camera-d16-3y77oooz/wing-x.png) · [等轴测](../../tmp/viz-camera-d16-3y77oooz/wing-iso.png)
- 同时运行的另一数据源可独立保存自己的镜头。切回原会话仍读到两个原书签。总计读回七张独立保留 PNG 和一张默认截图。会话关闭后复查已保留文件的摘要，另一个账号读取会话、书签和图片均返回 404。[原生 PNG 和所有权回执](../../tmp/viz-camera-d16-3y77oooz/camera-bookmarks-browser.json)
- 首轮功能断言均通过，切换时出现未捕获的浏览器异常。诊断轮确认旧会话的图像观察者在 WebSocket 关闭后尝试注销。工作台改为按会话标识挂载独立 iframe，切换时卸下旧文档。用新构建执行原验收，三个渲染进程的关闭均核验范围已清空，隔离 API 已退出且原端口关闭。[首轮记录](../../tmp/viz-camera-d16-27e8cu2y/camera-bookmarks-browser.json) · [带 WebSocket 诊断的记录](../../tmp/viz-camera-d16-uaiz4nbi/camera-bookmarks-browser.json) · [工作台实现](../web/src/panels/VizPanel.tsx)

同门禁的中间重跑曾在读取已保存书签时遇到一次本地 API 超时，保留当次回执。正式验收的只读请求均在首次完成。[中间重跑回执](../../tmp/viz-camera-d16-_s7n_ndp/camera-bookmarks-result.json)
