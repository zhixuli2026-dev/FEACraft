# Texo 屏幕框选

点击「屏幕框选」调用浏览器的共享选择器。只有收到视频首帧后才允许选区，框选位置按视频实际画面的比例记录。拖动到边缘的范围限制在当前画面内。截取后立刻停止所有轨道，再将 PNG 交给本地公式识别。选择器取消、源关闭、主动停止和面板卸载都会清理各自的捕获状态。 [Screen Capture API](https://w3c.github.io/mediacapture-screen-share/) · [面板实现](../web/src/panels/ViewerPanel.tsx)

2026-11-24，实际 Windows Edge `getDisplayMedia` 获得独立测试标签页的视频轨道，`displaySurface=browser`，源缩放 125%，帧大小 2198 × 1152，无音频轨道。对缩小的预览反向拖动得到 175 × 55 PNG，实际 FormulaNet 识别为 `E=mc^{2}`。裁剪、点击「停止共享」、关闭源标签后，均独立读回轨道为 `ended`。第二个独立浏览器调出原生共享选择器，经 Windows 键盘发送 Esc 并验证页面恢复可操作状态。实际 FormulaNet 1 次，实际与模拟 LLM 各 0 次。 [原始结果](../../tmp/ff-e20-texo-capture-pnnarwn5/texo-native-capture.json) · [裁剪公式](../../tmp/ff-e20-texo-capture-pnnarwn5/cropped-formula.png) · [验收入口](../server/texo_capture_browser_acceptance.py)

同日复验使用 Windows 唯一的 3440×1440 显示器。真实 Edge 158 获得 `displaySurface=monitor` 视频轨道，实际帧为 2752×1152。在原生置顶测试窗口放置公式，通过实际视频采样核验来源，再从缩放预览框选出 210×84 PNG。实际 FormulaNet 识别为 `E=mc^{2}`，裁剪后轨道状态为 `ended`。本轮还复验了浏览器标签页、主动停止、关闭源标签和原生选择器 Esc 取消。保存两张裁剪公式，共 2 次 FormulaNet，LLM 0 次。测试浏览器使用 Chromium 的显示器自动选择开关，用户工作台仍使用原生共享选择器。 [最终结果](../../tmp/ff-e20-texo-capture-3kqmu4q1/texo-native-capture.json) · [显示器裁剪原件](../../tmp/ff-e20-texo-capture-3kqmu4q1/cropped-monitor-formula.png) · [Chromium 测试开关](https://chromium.googlesource.com/chromium/src/+/d4afc97b7/chrome/common/chrome_switches.cc)

### 多屏切换验收

同日，在非特权 WSL 上启动隔离 X11 会话。XRandR 实际报告 800×600 与 640×700 两块虚拟显示器，每块显示不同的公式和颜色标记。用实际 Chromium 原生共享选择器在同一浏览器内依次选择 Screen 1 和 Screen 2。每次从真实视频核对颜色标记，调用产品的 `normalizeCrop` 处理反向拖动，保存约 212×61 的公式区，再核对相应轨道为 `ended`。Windows 的物理显示器和完整工作台验收见上文。 [原生选择和轨道](../../tmp/ff-e20-multimonitor-20261124/multimonitor-result.json) · [原生选择器试验图](../../tmp/ff-e20-multimonitor-20261124/isolated-monitor-picker.png)

使用当前前端发布包内的 FormulaNet 浏览器 WASM 识别两份真实裁剪 PNG。回读原始 LaTeX 的 `\mathsf` 字形命令，对照原图核验 `E=mc^2` 和 `a^2+b^2=c^2`。保留字形命令的逐 token 结果与公式内容核对分别记录。实际 FormulaNet 2 次，LLM 0 次。按 T07 完成 FF-E20。 [截图摘要与公式](../../tmp/ff-e20-multimonitor-20261124/t07-multimonitor.json)
