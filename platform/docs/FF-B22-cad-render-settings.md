# FF-B22 Zebra 配色与 Studio 纹理映射

2026-11-19 已在真实 Windows CadQuery 和 Edge 验收。

CAD 设置的 Zebra 工具可以调整条纹数、透明度、方向和反射／法线映射。选择「自定义」后，两枚颜色选择器分别修改交替的两种条纹颜色。切换回黑白、彩色或灰度时，可再次返回原来的自定义颜色。奇数条纹沿用首尾半宽规则。[平台配色实现](../web/src/utils/cadZebra.ts)

Studio 可为当前预览加载本地 PNG、JPEG 或 WebP。默认的「三平面投影」依据曲面法线混合三个方向的采样。选择「参数化 UV」后，有 UV 的对象使用源曲面坐标，其余对象沿用三平面投影。图片保留在当前算例的浏览器会话内，选择另一算例或点击「移除纹理」即释放。[平台映射实现](../web/src/utils/cadStudioTexture.ts) · [原映射模式](../../extracted/resources/cadviewer/vendor/three-cad-viewer/three-cad-viewer.esm.js#L84799)

几何脚本可在既有 `ctx.show` 或 `show_object` 调用中提供 `material`。原生 OCP 预览为带标签的曲面生成 UV。几何对象树仍以「平铺」开启，选择「装配」即可使用发布中的曲面坐标。已有装配和直接发布带 UV 的对象均提供该视图。带 UV 的表面经剖切时沿交点插值，关闭剖切后恢复原网格与坐标。[原生几何预览](../server/app/services/geo_script.py) · [剖切交点实现](../web/src/utils/cadClip.ts)

## 实际验收

真实 CadQuery 发布了两个几何对象。左侧曲面的 5,552 组 UV 从原生 OCP 产物读回，右侧发布未携带 UV。三平面纹理使左、右对象分别有 4,471 和 4,448 个像素发生变化。切换到参数化 UV 模式后，左侧 3,296 个像素改变，右侧 0 个。对照图可见两种映射都在真实网格上渲染。[三平面投影](../../tmp/geometry-render-browser-9sqehnfz/studio-triplanar.png) · [参数化 UV](../../tmp/geometry-render-browser-9sqehnfz/studio-parametric.png) · [实际控件](../../tmp/geometry-render-browser-9sqehnfz/studio-controls.png)

Edge 验证了三个原有配色、自定义双色编辑、反射与方向、超出条纹数范围时保留画面及关闭后恢复。无效图片保留上一次纹理，关闭再开启 Studio 与结束剖切均恢复画面。移除图片和切换算例清除纹理。前后读取的源发布摘要一致，另一账户读取返回 404。浏览器运行错误、着色器错误、真实模型调用和模拟模型调用均为 0。[自定义条纹](../../tmp/geometry-render-browser-9sqehnfz/zebra-custom-edited.png) · [浏览器结果](../../tmp/geometry-render-browser-9sqehnfz/geometry-render-browser.json) · [原生发布和权限回执](../../tmp/geometry-render-browser-9sqehnfz/geometry-render-result.json)

正式前端构建成功。Node 的五组断言涵盖原有配色、参数边界、自定义颜色首尾半宽和剖切 UV 插值。Windows 作业的 6 个进程均有退出证明，隔离 API 与原生工作进程独立核验退出。最终构建前后 C 盘可用空间均为 21.82 GiB，Docker 的 11 个运行容器和 0 B 构建缓存保持稳定。[正式入口](../server/geometry_render_settings_browser_acceptance.py) · [精确源审查](../../tmp/FF-B22-cad-rendering-stage/source-review-v5-shared.json) · [构建日志](../../tmp/FF-B22-cad-rendering-stage/v5-web-build.log) · [Node 断言](../../tmp/geometry-render-browser-9sqehnfz/render-primitives.log) · [退出核验](../../tmp/geometry-render-browser-9sqehnfz/cleanup-independent.json)

早期实际运行依次发现直接 OCP 的 UV 视图入口缺失、浏览器启动等待和相机折叠入口两处夹具问题，以及三平面着色器声明与预处理指令拼接问题。这些回执保留，最终结果来自修复后的独立运行。[原生视图](../../tmp/geometry-render-browser-4y1q5vvy/geometry-render-result.json) · [启动等待](../../tmp/geometry-render-browser-rp63k_fb/geometry-render-result.json) · [相机入口](../../tmp/geometry-render-browser-hvj5k54v/geometry-render-result.json) · [着色器编译](../../tmp/geometry-render-browser-0ib0wfrp/geometry-render-browser.json)
