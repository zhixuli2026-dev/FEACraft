# PPT 交付与验收

FF-E21 与 FF-F65 共用以下子任务。每项使用可下载的实际 PPTX 或媒体产物，完成后记录产物哈希和可复跑入口。 [交付计划](full-function-delivery-plan.md) · [PPT 工作副本与发布](ppt-publication.md)

| 子任务 | 交付与验收 | 状态 |
| --- | --- | --- |
| FF-E21.01 | 编辑原生文本、表格、图形和图片。复杂模板需验证母版、主题、版式、已有页和占位符的读回及渲染。 | 复杂模板实际作业与五页渲染通过 |
| FF-E21.02 | Beautify 可对页面应用排版、字体、间距与配色。对照源内容逐页检查样式和可编辑性。 | 真实后台作业、表格与页面渲染通过 |
| FF-E21.03 | 标准 187 个预设逐项保存原生几何，核验边界、描边、填充和渲染。 | 187/187 原生几何回读，187/187 实际渲染 |
| FF-E21.04 | 从参考图片重建可编辑文本和矢量对象，读回对象、位置、颜色并对照页面。 | 双语文字、实色矩形与椭圆通过 |
| FF-E21.05 | 可编辑讲稿、实际旁白和视频输出。核对音视频轨道、页序、时长及下载。 | 英语和普通话作业、Edge 播放和下载通过 |
| FF-E21.06 | 由实际演示渲染器输出每页，核对预设与复杂模板的页面证据。 | 已实际渲染预设、模板、美化和重建样本 |

`GET /api/v1/cases/{id}/ppt/presets` 返回 187 个稳定子编号及 DrawingML 预设名称。原生图形可通过 `POST /api/v1/cases/{id}/ppt:build` 的页级 `native_shapes` 数组添加。每个图形提供 `preset`、`x`、`y`、`width`、`height`，坐标是占幻灯片宽高的 0 至 1 比例。`fill` 接受 `#RRGGBB` 或 `null`，连接线只用 `stroke`。现有 `shape` 字段继续设置页首的小图形。算例面板支持预设选择、填充、描边和位置调整。

187 个预设名称和编号与 Open XML SDK 清单、Apache POI 中的对应 DrawingML 几何定义核对一致。实际后台作业生成 12 页 PPTX，逐一读回 `a:prstGeom`、原生对象类别和颜色。Edge 显示完整目录，拒绝超出页边界的位置，生成并下载具有指定填充和描边的齿轮。 [Open XML SDK 字段](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.drawing.shapetypevalues?view=openxml-3.0.1) · [逐项原生对象清单](../../tmp/ff-e21-ppt-presets-a7zrbog_/preset-manifest.json) · [PPTX](../../tmp/ff-e21-ppt-presets-a7zrbog_/ppt-presets.pptx) · [页面操作](../../tmp/ff-e21-ppt-presets-a7zrbog_/preset-controls.png)

非特权 WSL 真实运行 LibreOffice Impress 24.2.7 和 Poppler。12 张预设页的对应区域均检测到原生对象的着色像素。另一份真实 PPTX 作业保留两张模板页、原生图表、自定义主题、两种选定版式和备注，并追加一张美化数据页。首轮 PDF 暴露占位符双重项目符号，改用原生项目符号属性后复验全部 5 页。 [187 项渲染证据](../../tmp/ff-e21-ppt-presets-a7zrbog_/preset-render.json) · [模板验收](../../tmp/ff-e21-ppt-template-sgehnwnv/ppt-template.json) · [模板 PDF](../../tmp/ff-e21-ppt-template-sgehnwnv/published-deck.pdf) · [美化样本](../../tmp/FF-E21-ppt-20261124/comparison-beautify.png)

算例面板可选择已有图像或读取本地图片。Edge 使用同源 Tesseract WASM 和语言数据识别文本，像素分析重建实色矩形与椭圆。两张实测样本各包含一段文本和两个图形。人工改正后，生成文件的文本框和矢量对象均可编辑，用 Impress 渲染后读回改正内容。照片、复杂曲线、透明叠加和字体识别仍属现有重建能力的边界。实际本地 OCR 2 次，实际与模拟 LLM 调用各 0 次。 [Tesseract 本地部署](https://github.com/naptha/tesseract.js/blob/master/docs/local-installation.md) · [重建验收](../../tmp/ff-e21-ppt-reconstruction-rtq8umua/ppt-reconstruction.json) · [英文页面](../../tmp/ff-e21-ppt-reconstruction-rtq8umua/rebuilt-eng.png) · [中文页面](../../tmp/ff-e21-ppt-reconstruction-rtq8umua/rebuilt-chi_sim.png)

在算例面板选择带演讲者备注的 PPTX 和旁白语言即可生成 MP4。后台将 PPTX 封存，使用 LibreOffice Impress、Poppler、eSpeak NG、FFmpeg 和 FFprobe 输出 1280 × 720 的 H.264/AAC 视频。运行环境需安装这些工具，Windows 使用已配置的非特权 WSL。英语 17.82 秒、普通话 13.36 秒，两份实际视频的首尾帧与原页顺序一致，解码均读到有效音量。Edge 从有版本校验的发布入口预览并下载。现有输出路径会拒绝覆盖，原始 PPTX 哈希保持。 [播放验收](../../tmp/ff-e21-ppt-narration-edr6lhox/ppt-narration.json) · [英语 MP4](../../tmp/ff-e21-ppt-narration-edr6lhox/english-narration.mp4) · [普通话 MP4](../../tmp/ff-e21-ppt-narration-edr6lhox/chinese-narration.mp4) · [六项验收汇总](../../tmp/FF-E21-ppt-20261124/ppt-complete.json)
