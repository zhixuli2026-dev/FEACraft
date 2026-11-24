# FF-D10 场景脚本的 VF 函数

算例保存 `D-post/scenes.py` 后，当前原生 VTK 会话执行场景函数。创建函数返回对象名，同名更新复用渲染对象。场景中可以查询或删除对象，也可以重建滤波结果、切换时刻、恢复相机取景和保存 PNG。[场景函数实现](../server/app/services/viz_kernel.py) · [场景文件监听](FF-D09-scene-script-watcher.md)

| 函数 | 核验内容 |
| --- | --- |
| `VF_load` | 载入会话当前的 VTK 时序结果，或显式载入算例内的 OpenFOAM 目录。 |
| `VF_surface`、`VF_slice` | 实际提取表面及指定 X 坐标的切面，核对点与单元数。 |
| `VF_contour` | 以指定标量值提取表面的等值线并生成管形网格。 |
| `VF_stream`、`VF_glyph` | 在含非零速度矢量的参考网格上输出流线和箭头。 |
| `VF_color`、`VF_time` | 修改已有对象的染色场，按最接近的时刻加载实际字段并重建对象。 |
| `VF_reset_camera`、`VF_shot` | 从偏离网格的视角重新取景，读取两张原生 PNG 并比较实际像素。 |
| `VF_list`、`VF_remove` | 读取排序后的对象名，移除指定对象，并在同一会话重建切面。 |

验收经工作台文件树启动原生会话，经授权文件 API 保存场景，再从 Edge 对象树、数据信息、执行日志和原生 PNG 读回结果。两份 VTU 使用真实 VTK 生成参考字段和体网格。OpenFOAM 复用 C05 的 0.2、0.5 秒真实求解输出，上传前核对十个网格、控制和字段文件的 SHA-256。[验收入口](../server/viz_scene_facade_browser_acceptance.py) · [C05 证据](../../tmp/icofoam-c05-ghkszg16/icofoam-contract.json)

## 实际验收

2026-11-19 在 Edge 157 和 Windows 原生 VTK 完成。同一个会话先读取 VTU 时序，后显式切换到真实 icoFoam 数据。验收退出码为 0，浏览器错误和模型请求均为 0。异主保存场景和打开会话均返回 HTTP 404。关闭记录确认原生执行范围已清空，独立 API 已退出。[完整回执](../../tmp/viz-facade-d10-277fi3i_/vf-facade-result.json)

- 表面、切面、等值线管、流线和箭头分别产生 392、80、156、96、3,510 个单元。首次切面在 X=1.2。把表面染色场改成 `Tracer`，检查属性面板。输入 0.23 和 0.93 后，实际加载 0.2 与 1.0 时间步，温度范围从 [20, 32] 变为 [22, 34]。[浏览器回执](../../tmp/viz-facade-d10-277fi3i_/vf-facade-browser.json)
- 相机偏离网格时输出单色 PNG，脚本恢复取景后可见带标量色带的网格和矢量。两张原生 PNG 同为 384×364 像素，后一张有 1,164 种颜色。[偏离时截图](../../tmp/viz-facade-d10-277fi3i_/vf-camera-away.png) · [恢复后截图](../../tmp/viz-facade-d10-277fi3i_/vf-camera-reset.png)
- 指向缺失的 OpenFOAM 目录时报告失败并保留原对象。之后同一会话删除旧切面与箭头，并在 X=1.95 重建切面。显式读取有效的 OpenFOAM 目录后，VTK 读到 882 个源点、0.2 和 0.5 两个时刻以及 `p` 和 `Umag` 字段，并渲染 0.2 秒的压力表面。[执行日志](../../tmp/viz-facade-d10-277fi3i_/native-facade.log) · [恢复后的工作台](../../tmp/viz-facade-d10-277fi3i_/vf-facade-workbench.png)

第一轮已取得五种滤波输出，验收脚本误把输入框的值当文本读取而中止，回执同时记下未定位的 `Object` 页面错误。修正为读取输入框的值后完成上述矩阵。本项沿用现有渲染器实现。[首轮回执](../../tmp/viz-facade-d10-_9tcee4j/vf-facade-browser.json)
