# FF-B07 Base64 几何场景与显示接口

`vfLoadSceneFromBase64` 解码 UTF-8 JSON。加载时依次识别 `ocp`、`ocpMessage` 和直接的 `type: "data"` 消息。有有效 OCP 网格时优先显示它。遇到损坏的 OCP 数据时，有有效普通网格即可回退，否则保留上一有效画面并提示原因。单独传入普通空对象列表可清空当前视图，点击「重载场景」可恢复服务器发布。

OCP 路径解析三角网格叶节点的顶点、三角索引、法线和边线段，支持打包的数值缓冲，以及旧格式的数值数组。它按层级组合平移与四元数旋转。同一个网格实例可供多个叶节点引用，各自计算变换后的包围盒。普通 HTTP 场景加载继续读发布中的 `objects`。线与点原语、贴图和专有查看器配置属于其他功能范围。[解码实现](../web/src/utils/cadScene.ts) · [几何面板](../web/src/panels/GeoPanel.tsx)

真实 Edge 读取由 CadQuery 发布的两层装配。两个中文叶节点复用同一个原生网格引用，屏幕分别显示中心 `(10, 23, 2)`、`(9, 18, 6)`，尺寸 `4 × 2 × 6`、`2 × 4 × 6`。每个叶节点显示 24 个顶点和 12 个面。本轮浏览器验证了原生打包的数据和普通平铺数组，以及 OCP 三种入口、损坏引用的回退、错误输入保留上一帧和清空后重载。两个实际画面分别显示三个原生物体和一个普通回退物体，PNG 摘要与验收回执一致。[原生装配画面](../../tmp/geometry-base64-browser-0j1vz3sh/cad-native-ocp.png) · [普通网格回退画面](../../tmp/geometry-base64-browser-0j1vz3sh/cad-flat-fallback.png)

真实截图验证了边线、透明、主题和缩放变化。调用 `vfFit` 后，画面摘要与缩放前一致。所有本地加载前后，正式场景接口返回同一发布和完全一致的文档。隔离账号请求该场景收到 404，模型调用为 0。[通过回执](../../tmp/geometry-base64-browser-0j1vz3sh/geometry-base64-result.json) · [验收入口](../server/geometry_base64_browser_acceptance.py)

运行结束后，只读核对确认 API 端口已无监听，API 进程与原生根进程均已退出。作业与尝试记录均成功，退出码为 0。Windows Job 的持久清理回执验证 6 个成员全部退出，待确认数为 0，运行目录关联进程为 0。[API 启动日志](../../tmp/geometry-base64-browser-0j1vz3sh/api.log)
