# Color Map 自由控制点（WP07 / D06）

本文记录标量色图的编辑、透明度、色阶和文件交换。现有 v1 JSON 可以继续导入和导出。

## 1. 数据格式

`D-post/colormap.json` 使用 `vibeflow.colormap.v1`：

```json
{
  "schema": "vibeflow.colormap.v1",
  "points": [
    {"position": 0, "color": "#0000FF"},
    {"position": 0.5, "color": "#FFFFFF"},
    {"position": 1, "color": "#FF0000"}
  ]
}
```

规则：

- 控制点数量为 2–32，首尾 position 必须为 0 和 1，中间严格递增，不能重复。
- 颜色支持 `#RRGGBB` 或 `[r,g,b]` 浮点通道，统一规范化为大写十六进制。
- position/color 必须有限且在允许范围内；非法 JSON、错误 schema、点数不足、越界和重复位置均拒绝，不静默修正。
- LUT 在相邻控制点间逐 RGB 通道线性插值，默认采样 256 档。反转即逆序采样，对数轴和手动范围沿用现有设置。

`vibeflow.colormap.v2` 增加颜色空间、等宽色阶、每个色点的 `opacity` 与独立 `opacity_points` 曲线。`levels: 0` 使用连续色带，2–256 指定离散色阶数。颜色空间包括 RGB、HSV、Wrapped HSV、Lab、Diverging、Lab/CIEDE2000 和 Step。下面是一份由两端颜色、两端透明度和三个色阶组成的配置。

```json
{
  "schema": "vibeflow.colormap.v2",
  "space": "HSV",
  "levels": 3,
  "points": [
    {"position": 0, "color": "#FF0000"},
    {"position": 1, "color": "#00FF00"}
  ],
  "opacity_points": [
    {"position": 0, "opacity": 0.2},
    {"position": 1, "opacity": 0.8}
  ]
}
```

配置 `opacity_points` 时，VTK 使用该曲线查询透明度。否则在颜色点自带的 `opacity` 之间插值，省略的点取 1。全局不透明度与标量色带的透明度相乘。v2 支持 2–512 个颜色点和独立透明度点，输入文件限 256 KiB。

## 2. viz 操作

在色图“预设”中选择 `custom`：

1. 编辑控制点 JSON，点击“应用控制点”。
2. “默认”恢复蓝—白—红三点渐变。
3. “导出”把当前规范化结果写到 `D-post/colormap.json`。
4. “导入”读取该文件并应用。
5. 选择颜色空间、填色阶数后点击“应用映射设置”。实时色带显示当前插值、色阶和透明度。
6. 使用“导入 ParaView XML”读取 `D-post/colormap.xml`，使用“导出 ParaView XML”将当前颜色预设保存到同一路径。

自由控制点与预设、反转、对数轴、范围和不透明度共用 LUT。主管线和各场景对象在自动模式使用各自的原始标量范围。输入错误时界面报告原因，当前可用色带仍可继续使用。

ParaView XML 支持包含一份连续标量色图的 `ColorMap` 或 `ColorMaps` 文件。导入时将实际坐标按首尾归一到 `[0,1]`，导出保留颜色空间和原颜色点，并在 `o` 字段写入该点处的透明度。完整的色阶数及独立透明度曲线使用 v2 JSON 保存。ParaView 5.11.2 的旧 XML 预设导入按 RGB 控制点设置颜色，独立透明度在 ParaView 中通过自身曲线保存。[ParaView 5.11 色图格式](https://docs.paraview.org/en/v5.11.0/Tutorials/ClassroomTutorials/beginningColorMapsAndPalettes.html) · [ParaView 5.11.2 预设导入实现](https://raw.githubusercontent.com/Kitware/ParaView/v5.11.2/Remoting/Views/vtkSMTransferFunctionProxy.cxx)

## 3. 验证结果

已由 `colormap_smoke.py` 验证：

- 三点分段插值、反转和边界颜色。
- v1 JSON 规范化与无损往返。
- 点数、端点、顺序、重复、颜色和 position 范围负向输入。

`e2e-colormap.mjs` 已验证 VTP 实际渲染、Vuetify/native select 兼容操作、custom LUT 和导出文件；主 `e2e-drive.mjs` 中的色图断言也通过。`shots/wp07-colormap-focused.png` 已人工核对，黑—黄—黑渐变及 `D-post/colormap.json` 导出状态可见。

高级色图验收使用真实 Edge、635 点及 504 单元的 VTK 标量网格、本机 ParaView 5.11.2。对七个实际 VTK 像素做背景混合核对，透明度的最大通道误差为 1。HSV 在原生 VTK 截图和 Edge 画布中均显示三种色阶，三段中心相差最多 1 个 RGB 通道值。JSON 保留色阶及独立透明度。ParaView 读回 Lab XML 的两个色点和实际中点 `[0.784314, 0.670588, 0]`。非法 JSON/XML 后可继续使用先前色带，跨账号访问为 404，已核验 Viz 会话原生停止和隔离 API 退出。此项模型调用为零。[验收入口](../server/viz_colormap_browser_acceptance.py) · [正式回执](../../tmp/viz-colormap-d06-rlk7nuzr/colormap-result.json) · [Edge 工作台](../../tmp/viz-colormap-d06-rlk7nuzr/colormap-workbench.png) · [VTK 三段色阶](../../tmp/viz-colormap-d06-rlk7nuzr/native-hsv-three-bands.png)

更新 LUT 后复验对象管线和数据信息。D02 首轮通过功能与三个会话停止检查，另记录一次来源未定位的 `Object` 页面异常，因此退出 1。同一套代码、构建和断言重跑通过，页面异常为零。D04 实际 ParaView、三类网格及三个原生会话退出一次通过。两项回归模型调用均为零。[D02 首轮回执](../../tmp/viz-pipeline-d02-vz3l7tfs/pipeline-browser.json) · [D02 通过回执](../../tmp/viz-pipeline-d02-m3mri8me/pipeline-result.json) · [D04 通过回执](../../tmp/viz-information-d04-ixe4jev_/viz-information-result.json)
