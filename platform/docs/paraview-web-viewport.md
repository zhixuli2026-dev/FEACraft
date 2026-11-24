# ParaView 原生工作区与 Trame

在算例的「可视化」面板选择「ParaView 原生工作区」，再从求解目录启动，或从结果文件和来源与过滤器面板打开结果。原生工作区包含管线树、对象属性、来源和过滤器、字段、时间、相机和导出。选择「VTK 交互会话」可通过 Trame 打开同一份结果文件。默认引擎为 Trame。

## 部署与接口

Linux ParaView 镜像使用安装包内的 ParaViewWeb Visualizer、原生 Server Manager、wslink 与 OSMesa。`GET /api/v1/cases/{id}/viz:engines` 报告当前 API 实例安装的引擎。`POST /api/v1/cases/{id}/viz:sessions` 接受可选的 `engine` 字段，值为 `trame` 或 `paraview`。现有 Windows 安装提供 Trame，原生选项在装有 Visualizer 的实例上显示。Linux 容器的 `VIBEFLOW_VTK_OSMESA_DIR` 只为 Trame 的渲染子进程提供镜像已有的 OSMesa 库路径。 [Linux 镜像和工具链来源](linux-paraview-deployment.md)

两种引擎共用会话归属校验、启动就绪记录、HTTP 和 WebSocket 代理及进程退出证据。Visualizer 在同源 iframe 中连接该会话的 `/viz-proxy/{sid}/ws`。原生文件面板使用算例内相对路径，导出写入 `D-post/visualizer/{session}`。通用 XML 属性、完整来源与过滤器目录及插件操作分别按 FF-D03、FF-D05、FF-D20 验收。

## T06 实测

Edge 在实际 Linux ParaView 5.11.2 容器上通过界面生成 Cone VTP，打开原生工作区，接收鉴权代理传递的二进制图像帧，旋转相机并调整管线对象可见性。未认证访问返回 401，另一已认证账号返回 404。随后用原 Trame 在同一算例渲染同一个 VTP。两个会话均验证归属范围退出。首次浏览器发现 Visualizer 默认连接站点根 `/ws`，随后改用会话路径。Trame 首次缺少图形库搜索路径，复用已有 OSMesa 后复验通过。 [验收记录](../../tmp/FF-D01-native-viewport-20261123/d01-acceptance.json) · [原生渲染](../../tmp/FF-D01-native-viewport-20261123/native-edge-frame.png) · [Trame 渲染](../../tmp/FF-D01-native-viewport-20261123/trame-edge-frame.png)

真实和模拟模型调用均为 0。测试复用本机 Docker 镜像，未新增镜像和未手动回收 Docker 缓存。 [Windows 前](../../tmp/FF-D01-native-viewport-20261123/before-c.json) · [Windows 后](../../tmp/FF-D01-native-viewport-20261123/after-c.json) · [Docker 前](../../tmp/FF-D01-native-viewport-20261123/before-docker.txt) · [Docker 后](../../tmp/FF-D01-native-viewport-20261123/after-docker.txt)
