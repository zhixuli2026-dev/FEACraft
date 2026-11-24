# FF-B09 CAD 相机、网格与 Studio 渲染

2026-11-19 已在真实 Windows CadQuery 和 Edge 中验收。

## 使用方式

- 「视角与截图」提供等轴测、+X、+Y、+Z 和 Fit。对象设置可独立开启 XZ、XY、YZ 网格，并将网格中心切换到当前几何中心。
- 「视图」切到四宫格后，点击或用 Tab 聚焦目标视口。拖动、滚轮、Orbit／Trackball、透视／正交、相机预设和 Fit 均作用于该视口。切回单视图可继续使用原相机，重新进入四宫格会恢复各相机的位置和投影。
- 「Studio 环境」可选择程序生成影棚或 HDR 环境图，调节环境强度、旋转、曝光和 PBR 中性／ACES Filmic／线性色调映射。背景可使用环境、透明、渐变或纯色。选择照片环境图后可切换 1K 和 4K。照片由浏览器从 Poly Haven 读取。加载失败时保留上一张已显示的环境图，可重新选择。程序生成影棚可离线使用。[环境图库许可](https://polyhaven.com/license)
- 「阴影」支持强度、柔度和接地面。单视图可叠加 SSAO，并调节 AO 强度。开启四宫格会关闭 SSAO。已有 Studio 表面纹理继续支持三平面和参数化 UV。[纹理映射](FF-B22-cad-render-settings.md)
- 「表面材质」包含标准、亮面清漆、透明玻璃和缎面光泽预设，可调清漆、清漆粗糙度、透射、折射率和光泽。选中几何对象时，设置只应用于它。未选中对象时，使用整个算例的默认表面配置。[CAD 面板](../web/src/panels/GeoPanel.tsx)

## 实际验收

真实 CadQuery 发布了倒角底座、外壳和环形窗口。Edge 拖动、滚轮分别只改变第一个视口。聚焦第四个视口后，切换 Trackball、正交、+Y 和 Fit 时，其余三个相机保持位置和朝向。三面网格及居中使 32,984 个像素产生变化。[独立视口](../../tmp/geometry-studio-view-yz3mvkib/quad-independent-projection.png) · [三面网格](../../tmp/geometry-studio-view-yz3mvkib/centered-three-grids.png)

Edge 对同一环境的 1K 和 4K HDR 分别收到实际 HTTP 200 和 1,508,872／23,545,064 字节。已绑定纹理分别为 1024 × 512 和 4096 × 2048。两种分辨率的画面中有 115,101 个像素在至少一个颜色通道上产生变化。旋转改变了 75,798 个像素，两次色调映射切换分别改变 20,139 和 78,637 个像素。通过同一浏览器验证了曝光、背景切换、断网后的环境保留与程序影棚恢复。[1K 渲染](../../tmp/geometry-studio-view-yz3mvkib/studio-photo-1k.png) · [4K 渲染](../../tmp/geometry-studio-view-yz3mvkib/studio-photo-4k.png) · [环境旋转](../../tmp/geometry-studio-view-yz3mvkib/studio-photo-rotated.png)

阴影强度、阴影柔度和 AO 强度分别改变了 453、814 和 508 个像素。单独给窗口应用玻璃材质改变了 2,057 个像素。底座、外壳的透射值保持为 0，随后回读清漆、光泽、折射率，并验证与原表面纹理联用。[阴影](../../tmp/geometry-studio-view-yz3mvkib/studio-shadow-soft.png) · [AO](../../tmp/geometry-studio-view-yz3mvkib/studio-ao-three.png) · [窗体玻璃](../../tmp/geometry-studio-view-yz3mvkib/studio-window-glass.png) · [浏览器详细回执](../../tmp/geometry-studio-view-yz3mvkib/studio-view-browser.json)

源场景摘要在全部操作前后相同，异账户读回返回 404。正式构建和四组 Node 断言通过。真实模型、模拟模型、页面运行错误和着色器错误均为 0。Windows 作业的 6 个进程均有退出证明，隔离 API 端口关闭。最终构建前后 C 盘可用空间均为 22.10 GiB，Docker 构建缓存为 0 B。[真实验收入口](../server/geometry_studio_view_browser_acceptance.py) · [55 项精确源审查](../../tmp/FF-B09-cad-rendering-20261119/source-review-v2.json) · [原生作业回执](../../tmp/geometry-studio-view-yz3mvkib/studio-view-result.json) · [独立退出核验](../../tmp/geometry-studio-view-yz3mvkib/cleanup-independent.json) · [Node 断言](../../tmp/geometry-studio-view-yz3mvkib/studio-view-primitives.log)

首轮正确取得两种实际 HDR 及纹理尺寸，截图对同一张环境图的差异低于通用 12 级通道差异。最终对分辨率切换使用精确通道差值，并保留 HTTP、尺寸与其他渲染对比断言。[首轮回执](../../tmp/geometry-studio-view-4t05d4y1/studio-view-browser.json)
