# CAD 场景与查看器接口

2026-11-19 已复验 Base64 本地场景、OCP 三角网格和显示接口，以及两个独立 API 上的场景同步与重连。实现位于 `server/app/routers/cadscene.py`、`web/src/panels/GeoPanel.tsx` 和 `web/src/utils/cadScene.ts`。[Base64 实测](FF-B07-base64-cad-scene.md) · [跨 API 验收](FF-B06-cadscene-replica.md)

## 1. 事实源与两条载入通道

- 服务器事实源是算例内的 `A-geo/scene.json`。自动预览和 `/ws/cadscene` 都通过它衔接；数据库不替代该文件。
- `GET /api/v1/cases/{id}/geometry/scene` 读取事实源，GeoPanel 校验后构建 Three.js 网格。
- Base64 是**仅当前视图**的本地载入通道：不写服务器文件、不触发预览作业，其他客户端不会因此收到 `scene.replace`。

场景替换先完整构建新网格，成功后才移除旧网格。Base64/JSON 解码、对象结构或面索引错误会保留上一有效场景并显示错误。预览 watcher 使用代次校验：旧成功回调完成异步加载后，不能清除更新作业的失败/运行状态。

## 2. cadscene WebSocket

每条 replace／clear 使用独立 DB 会话复核活跃用户与算例权限。文件与版本事件在同一写入事务中提交。客户端可使用 `expected_revision`、`expected_sha256` 和 `expected_version`。冲突返回 `type=error`，以及 `revision_conflict`、`file_conflict` 或 `scene_version_conflict`。采用 `snapshot=true` 可在 `ready` 后读取当前快照。连接恢复后 GeoPanel 依据版本顺序更新场景。[场景版本协议](FF-B06-cadscene-replica.md) · [原条件写入](case-write-coordination.md)

连接：

```text
/ws/cadscene?case_id=<id>&token=<JWT>
```

服务端校验 JWT、活跃用户和 case 归属。连接成功先收到：

```json
{"type":"ready","backend":"cadscene","case_id":1}
```

客户端消息：

```json
{"type":"scene.replace","objects":[{"name":"part","color":"#2563eb","alpha":1,"vertices":[[0,0,0],[1,0,0],[0,1,0]],"faces":[[0,1,2]]}]}
```

```json
{"type":"scene.clear"}
```

`scene.replace` 写入对象，`scene.clear` 写入空 `objects`。两个独立 API 从共享的持久事件和当前场景文件发送同一变更。非法颜色、alpha、向量、面索引或超限对象返回 `error`，保留此前有效场景。每条连接有独立的五秒发送时限。

## 3. Base64 场景格式

GeoPanel 挂载后提供：

```js
window.vfLoadSceneFromBase64(encodedScene);
```

输入是 UTF-8 JSON 的 Base64。支持：

- 原生 `scene.json` 的 `objects`；空 `objects` 也可表示空视图。
- `vertices`：嵌套三维点或扁平数值数组。
- `faces` 或 `triangles`：嵌套面/扁平三角索引；多边形按扇形三角化。
- `bbox: {min,max}` 或原版 fallback 的 `bb: {xmin,ymin,zmin,xmax,ymax,zmax}`；缺 bbox 时从顶点计算。
- `name`、`#RRGGBB` 颜色和 `[0,1]` 的 alpha。

最多 64 个对象；对象名 1–120 字符。每对象至多 100,000 个顶点和 200,000 个三角面，扁平与嵌套表示使用相同上限。多边形按展开后的三角面计数。非法 UTF-8、JSON、颜色或越界索引会显示诊断并保留上一帧。

Base64 文档含 `ocp` 或旧名 `ocpMessage` 时优先读取 OCP 网格，也可传入 `type: "data"` 消息。实体和曲面的三角网格接受普通数组和 OCP Base64 `float32` / `int32` 缓冲，解析引用实例、父子平移与四元数旋转，并使用网格自带的法线和离散边线。OCP 损坏时可按同一文档中有效的普通网格回退。否则保留上一有效渲染。服务器场景的平铺视图按 `objects` 读取，装配视图使用该发布的有效 OCP 网格。[对象树与装配视图](FF-B08-cad-object-tree.md)

## 4. Fit、边线、透明和主题

| 接口 | 当前行为 |
|---|---|
| `window.vfFit()` / 工具栏 `Fit` | 对可见 CAD mesh 求 bbox，按相机 FOV、aspect 和 1.2 边距计算距离；重复调用恢复同一取景 |
| `window.vfSetEdges(boolean)` / `边线` | 显示或隐藏各对象边线；对象自身可见性仍优先 |
| `window.vfSetTransparent(boolean)` / `透明` | 启用时 opacity 取 `min(baseAlpha, 0.5)` 并调整 depthWrite；关闭恢复 baseAlpha |
| `window.setVibeFlowTheme(theme)` / `主题` | `dark` 为深色，其余值为 `light`；更新场景背景、网格、边线颜色和主题属性 |

主题同步到 `document.documentElement`、`document.body` 和 CAD canvas host 的 `data-vf-theme/data-theme`；host 另有 `data-edges`、`data-transparent`。边线、透明和主题偏好由 ref 保存，Base64 或服务器场景重载后继续应用于新网格。

这些接口只控制 CAD 查看器，不是完整应用主题系统。

## 5. 相机、studio、阴影和 SSAO

- `正交` 在透视/正交相机间切换并重建控制器；Fit 按 FOV/aspect 或正交 span 计算，相机 far 随取景距离更新，避免深度精度掩盖小模型。
- `控制` 支持 Orbit 与 Trackball，切换时保留 target；实际指针拖动会改变视角。
- `视图` 支持单视图与四宫格。四宫格使用四个可独立操作的相机与控制器，通过 scissor 和 viewport 渲染。拖动、滚轮、控制类型、投影、预设及 Fit 作用于当前视口。
- `坐标网格` 可独立显示 XZ、XY 和 YZ 平面，可按几何中心定位。Studio 支持程序生成影棚与 1K／4K HDR 环境图、旋转、环境强度、曝光、色调映射和背景。表面材质提供实体及算例范围的清漆、透射、折射率和光泽控制。
- `阴影` 使用 DirectionalLight 和 ShadowMaterial 接地面。Studio 中可调阴影强度和柔度。
- `SSAO（单视图）` 可调强度，使用 EffectComposer、SSAOPass 和 OutputPass 合成原画面，与四宫格互斥。

Studio 纹理映射支持三平面投影、原生曲面 UV 和无 UV 时的投影回退。[相机与渲染验收](FF-B09-cad-studio-views.md) · [Studio 纹理验收](FF-B22-cad-render-settings.md)

## 6. 真实剖切与封闭端盖

`剖切` 不再只依赖 GPU clipping plane。GeoPanel 对源三角面执行平面裁剪，交点按容差焊接为闭环，外轮廓与内孔按嵌套深度分组，再用 `ShapeUtils.triangulateShape(contour, holes)` 生成真实端盖。平面助手只作辅助显示。

端盖网格与源对象共享可见性、边线、透明、爆炸、Zebra 和阴影处理；滑块变化只替换临时裁剪 geometry，关闭剖切恢复源 geometry。`data-clip-caps` 暴露端盖对象数用于断言，浏览器同时检查带孔平板的实际截图。

当前是三角化 CAD 网格的 CPU 实现，不是 B-rep 精确布尔运算；非流形/超大模型的拓扑和性能仍需更多验证。

## 7. 爆炸动画与互斥

爆炸位置由对象中心偏移计算，支持可关闭的时间插值过渡。`data-explode-progress` 记录 0–1 的真实进度，普通动画启动时立即归零爆炸进度，防止两个 RAF 同时写位置。当前没有独立的时长/缓动编辑器。

## 8. 关键帧轨道编辑器

`vibeflow.cadquery.animation.v1` 支持 t/tx/ty/tz/q/rx/ry/rz 八类轨道。轨道包含 selector、严格递增 times 和等长 values；t 为三维位置偏移，tx/ty/tz 为单轴偏移，rx/ry/rz 为角度（度），q 为单位四元数。采样支持线性插值和四元数 slerp，重复应用顺序与输入一致。

GeoPanel 提供 JSON 应用、播放/暂停/停止、时间 scrub 和导出。每帧从源 transform 重算，普通动画与轨道/爆炸互斥，非法 JSON 或未知 selector 不改变上一有效场景。不是完整 ParaView 全类型/属性轨道系统。

## 9. Zebra 配色与 mapping

Zebra 使用原版 reflection/normal shader。支持 `blackwhite`、`colorful`、`grayscale` 三种配色，奇数条纹首尾半宽和 colorful 色相连续；`count`、`opacity`、角度方向和 `mappingMode=reflection|normal` 可运行时调整。`opacity=0` 恢复对象 baseColor，关闭 Zebra 恢复原材质。透明 alpha、对象选择和端盖材质生命周期均覆盖。

选择「自定义」可编辑两种交替的条纹颜色。Studio 的纹理映射与 Zebra 的反射／法线映射分别由各自面板控制。[自定义配色与纹理投影](FF-B22-cad-render-settings.md)

## 10. 验证

在 `platform/server` 执行：

```bash
node ../tools/cad-scene-smoke.mjs
node ../tools/cad-view-smoke.mjs
node ../tools/cad-clip-smoke.mjs
node ../tools/cad-animation-smoke.mjs
node ../tools/cad-zebra-smoke.mjs
node ../tools/e2e-cad.mjs ../docs/shots/wp08-cad-focused.png
node ../tools/e2e-cad-view.mjs ../docs/shots/wp08-cad-studio.png
node ../tools/e2e-autopreview.mjs
```

纯函数套件覆盖 Base64 UTF-8、扁平/嵌套数据、bbox、透视/正交 Fit、控制/视图模式、材质范围、四宫格视口、真实剖切、端盖闭环/面积/法线和带孔轮廓。浏览器套件覆盖真实预览、cadscene、Base64 替换/恢复、服务器事实源不变、非法输入保留旧场景、像素变化/恢复、Fit 重复取景、Orbit/Trackball、studio、材质、四宫格、阴影、SSAO、真实端盖、爆炸中间进度/互斥、八类轨道播放/暂停/停止/scrub/导出、Zebra 配色/mapping/角度/opacity 及既有导出/高级交互。证据截图：`shots/wp08-cad-focused.png`、`shots/wp08-cad-focused-base64.png`、`shots/wp08-cad-studio.png`、`shots/wp08-cad-studio-quad.png`、`shots/wp08-cad-studio-shadow.png`、`shots/wp08-cad-studio-ao.png`。
