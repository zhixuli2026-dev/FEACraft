# FF-B11 CAD 三轴剖切与端盖

2026-11-19，几何预览支持在 X、Y、Z 三轴选择切面和保留侧。切面位置可用方向键微调，平面助手与实际切割同步。端盖法线朝向移除侧，轮廓闭合后按包含关系处理凹边和孔洞。轮廓断开时保留已经剖切的表面，并在控件旁提示轮廓未闭合。切换到空算例会移除旧切面和网格。

真实 Windows CadQuery 作业生成体积 596 的凹形双孔板。Edge 157 对每轴两个方向执行剖切，直接读取画布所用的 Three.js 端盖三角面。X、Y、Z 的端盖面积分别为 32、52、149，双方向法线均朝外。键盘将滑块从 50% 移至 51%，实际三角面和辅助平面随之移动。关闭剖切后恢复原网格对象，画布图像字节一致。[真实浏览器结果](../../tmp/geometry-section-browser-b46qwzin/section-browser-result.json) · [剖切控件](../../tmp/geometry-section-browser-b46qwzin/section-controls.png) · [双孔端盖](../../tmp/geometry-section-browser-b46qwzin/section-holes.png)

从同一真实发布派生的开边界网格保留剖切表面，端盖数量为零。对真实三角面细分后的 170,100 面、90,804 顶点网格，实测三次剖切和恢复各用 1.005、0.986、0.986 秒。恢复后 GPU 几何对象计数回到初始值，浏览器回收后的 JavaScript 堆增加约 0.39 MiB。性能样本来自本机 Edge 同一会话。无效场景载荷保留上一网格；后端读取确认原 CadQuery 场景摘要保持，其他账户读取返回 404。[开边界显示](../../tmp/geometry-section-browser-b46qwzin/section-open.png) · [后端回执](../../tmp/geometry-section-browser-b46qwzin/section-result.json)

直接几何检查覆盖立方体、四面体、全保留、全丢弃、带孔和开边界场景。正式前端构建通过，Impeccable 检测返回空列表。原几何作业停止已核验，隔离 API 的原进程已独立确认退出。真实模型及模拟模型调用均为 0。本轮验收范围为显示用三角网格剖切。[几何检查](../../tmp/geometry-section-browser-b46qwzin/clip-primitive.log) · [独立退出核验](../../tmp/geometry-section-browser-b46qwzin/cleanup-independent.json) · [登记入口](../server/geometry_section_browser_acceptance.py) · [审查散列](../../tmp/FF-B11-cad-section-20261119/source-review.json)
