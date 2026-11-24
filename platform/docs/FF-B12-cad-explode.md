# FF-B12 CAD 爆炸视图和动画切换

2026-11-19，几何预览增加 0.1 至 10 秒的过渡时长、匀速、缓入缓出和缓出选项。暂停会保留当前姿态和已经播放的时间，继续后完成剩余过渡。拖动分离滑块会从当前姿态调整到新目标。输入无效时保留上次有效时长。关闭过渡或启用系统减少动态效果时，视图直接切换到目标位置。

爆炸位移、普通旋转以及关键帧轨道通过同一视图互斥。普通动画在爆炸收回途中启动时立即恢复源位置并接管旋转。播放轨道会清除爆炸，开启爆炸会停止轨道并恢复源姿态。切换算例清除正在播放的爆炸和轨道位置。

真实 Windows CadQuery 作业发布包含七个可见对象的嵌套 OCP 装配。Edge 157 在正式前端中逐一读取实际 Three.js 网格及边线位置。匀速、缓入缓出和缓出均吻合所选曲线。2 秒匀速过渡在播放到约 0.476 秒时暂停，可见位置在观察期间保持，继续后实际播放时间为 2 秒。输入零显示纠正提示并沿用 2 秒。键盘修改分离量后，观察到网格从当前约 24% 的姿态转向 55%。关闭爆炸后画布 PNG 与原图逐字节相同。[浏览器结果](../../tmp/geometry-explode-browser-x0qqx2l1/explode-browser-result.json) · [暂停与校验截图](../../tmp/geometry-explode-browser-x0qqx2l1/explosion-paused-controls.png) · [恢复截图](../../tmp/geometry-explode-browser-x0qqx2l1/explosion-restored.png)

源场景从正式发布接口读回，操作前后 SHA-256 相同，其他账户读取返回 404。原生 CadQuery 作业停止已核验，隔离 API 的原进程已独立确认退出。正式前端构建和 Impeccable 检测通过，浏览器无运行错误，真实和模拟模型均调用 0 次。[后端回执](../../tmp/geometry-explode-browser-x0qqx2l1/explode-result.json) · [独立退出核验](../../tmp/geometry-explode-browser-x0qqx2l1/cleanup-independent.json) · [登记入口](../server/geometry_explode_browser_acceptance.py) · [审查散列](../../tmp/FF-B12-cad-explode-20261119/source-review.json)
