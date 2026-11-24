# FF-B13 CAD 关键帧与对象轨道

2026-11-19，几何预览保留 t、tx、ty、tz、q、rx、ry、rz 八类动作和原有轨道应用顺序。对象属性可按所选动作将零件添加到 JSON 编辑器，属性区也会列出该零件已应用的轨道。新建轨道使用 OCP 实例标识 `objectId`，原 v1 文档可继续使用唯一的名称选择器。同名或兼容匹配有歧义时返回提示，原姿态保留。

应用时解析并绑定当前场景实例，播放按已绑定的动作采样。重载当前场景后保留 JSON 草稿，重新应用才会绑定新网格。切换算例会清除原草稿及绑定。暂停时改动剖切，重建后的网格、边线和端盖沿用暂停位置与旋转。CAD 面板识别可视化相机轨道并提示对应入口。

真 Windows CadQuery 生成两个独立的同构算例及七个零件的嵌套 OCP 装配。Edge 157 实际读取画布使用的网格和边线。在 0.50 秒处，八类动作按给定顺序组合后，转子位移为 `[3, 1, 9]`，四元数与独立计算一致。旧式选择器给轴件位移 `[0, 2, 0]`，在稳定标识轨道里借用显示名称的轴承保持原位。截面重建后，转子端盖和端盖边线都保持同一暂停姿态。[数字验收](../../tmp/geometry-keyframes-browser-_3153hij/keyframes-browser-result.json) · [对象属性](../../tmp/geometry-keyframes-browser-_3153hij/tracks-object-link.png) · [暂停与剖切](../../tmp/geometry-keyframes-browser-_3153hij/tracks-paused-section.png)

真实浏览器还验证单次播放结束、循环回绕、暂停、停止和原画布逐字节恢复。非法实例、循环参数和相机文档均保留最后一次有效姿态。下载的 v1 JSON 保留新增实例标识，旧名称轨道可往返。两个源发布的摘要保持，另一账户访问返回 404。两个 CadQuery 作业停止已核验，隔离 API 原进程独立确认退出。前端构建通过，Impeccable 检测返回空列表。真实和模拟模型调用均为 0。[实际后端回执](../../tmp/geometry-keyframes-browser-_3153hij/keyframes-result.json) · [已应用文档下载](../../tmp/geometry-keyframes-browser-_3153hij/applied-tracks.json) · [直接几何和轨道检查](../../tmp/geometry-keyframes-browser-_3153hij/keyframe-primitives.log) · [独立退出](../../tmp/geometry-keyframes-browser-_3153hij/cleanup-independent.json) · [登记入口](../server/geometry_keyframes_browser_acceptance.py) · [审查散列](../../tmp/FF-B13-cad-keyframes-20261119/source-review.json)
