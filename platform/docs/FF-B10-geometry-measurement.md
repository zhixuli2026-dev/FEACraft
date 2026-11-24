# FF-B10 几何包围盒量测

在几何预览的 CAD 对象列表中选择对象，可在列表下方看到包围盒范围、三轴尺寸、中心坐标和发布网格的顶点及面数。对象名称可用鼠标或键盘选择，旁边的复选框独立控制可见性。

选择另一个对象后，面板显示两个包围盒中心的三维直线距离，保留三位小数。更新场景时，仍存在的主选对象保持选中。比较对象移除时清除比较和距离。同名对象再次出现后，需要重新选择它。

专用验收通过真实 Edge 操作界面，用隔离算例中的 CadQuery 输出核对量测读数、场景更新和算例权限。[验收入口](../server/geometry_measurement_browser_acceptance.py)

实际 Edge 以 Tab、Enter 和空格选择对象。第一版主件显示尺寸 `2 × 4 × 6`、中心 `1, -2, 0.5`、24 个顶点、12 个面。两对象中心距离为 `5.000`。两次编辑保存分别生成移除和重新引入比较对象的实际发布。主件选中状态保持，比较项在对象移除时清空。对象重新出现后，手动重选得到三维中心距离 `13.000`。切换到空算例清除读数，另一账户读取场景和验证均收到 404。回执保留带原生公差的坐标，两次保存产生的 CadQuery 作业均以 0 退出。[真实验收回执](../../tmp/geometry-measurement-browser-ft0aczrk/geometry-measurement-result.json) · [5.000 量测画面](../../tmp/geometry-measurement-browser-ft0aczrk/bbox-distance.png)

与 Base64 网格兼容修复和文件监听改进合并后，在同一份最终前端构建上再次通过真实 Edge 验收。三次实际发布、两组距离和两项跨账户 404 结果一致。三个 CadQuery 原生范围各有 6 个进程的退出回执，隔离 API 端口已关闭。[合并复验](../../tmp/geometry-measurement-browser-d6vrelhv/geometry-measurement-result.json) · [退出复核](../../tmp/geometry-measurement-browser-d6vrelhv/cleanup-independent.json)
