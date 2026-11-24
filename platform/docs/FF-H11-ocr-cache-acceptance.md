# FF-H11 公式识别缓存与旧记录维护

公式识别缓存按浏览器账户保留最近 20 条。每条回执绑定实际图片 data URL 的 SHA-256、FormulaNet、LaTeX 和生成时间。`localStorage` 与 `CacheStorage` 分别存一份快照，写入和清理按顺序执行。支持 Web Locks 的浏览器在不同标签间也使用同一把更新锁。解析失败、字段不符或张冠李戴的记录无法提供缓存命中。一份快照损坏时，另一份可恢复。两种持久存储都被限制时，仍可使用真实本地模型，同一面板用有界内存保留本次结果。[缓存实现](../web/src/utils/texoCache.ts)

旧单条回执仍按当前选中图片的确切哈希读取。核对哈希和回执字段后，写入当前账户快照，写入成功才清理旧副本。旧索引中超出 20 条的记录会在两种存储中清理。历史派生的 LaTeX 文本在新旧格式间不重写。回退时，旧代码继续识别并建立原格式缓存，未迁移的旧回执仍可读取。整个维护不影响算例图片、编辑器文件或本地模型资源。[模型页面](../web/public/texo/texo-ocr.html)

面板在启动识别时记录请求编号与图片哈希。切换图片的迟到结果保存在原图片名下，当前面板继续显示新选中的图片。算例图片读取失败时清空预览并给出重新选择提示。账号和算例切换重新建立面板，前一个面板的事件不会显示在当前上下文中。[界面入口](../web/src/panels/ViewerPanel.tsx)

## 实际验收

| 浏览器 | 实际识别与缓存 | 画面 |
| --- | --- | --- |
| Chromium | [6 项记录](../../tmp/texo-ocr-browser-95a0pgki/texo-browser.json) | [缓存命中](../../tmp/texo-ocr-browser-95a0pgki/texo-cache-hit.png) |
| Microsoft Edge | [6 项记录](../../tmp/texo-ocr-browser-a2mwv813/texo-browser.json) | [缓存命中](../../tmp/texo-ocr-browser-a2mwv813/texo-cache-hit.png) |

两个浏览器都使用生产构建和独立 API，实际下载本地 ONNX 模型，对画布生成的公式图进行推理。模型原样返回 `E \! = \! m c ^ { 2 }`。最终两轮共 4 次实际 FormulaNet 推理。再次选图或点击识别从缓存返回原 LaTeX，未再次进入模型。验证另包括切图竞争、真实算例图片下载、注入一次 HTTP 503 后重试、损坏的本地快照恢复、旧回执迁移、超额裁剪，以及第二账户的隔离。注入两种浏览器存储的错误时，真实推理仍运行并能在会话内命中。DeepSeek 调用 0，模拟识别 0。[浏览器验收入口](../server/texo_receipt_browser_acceptance.py)

早期用例先匹配到隐藏的原生选择项，尚未进入模型。随后 Chromium 和 Edge 都返回真实公式，驱动首次比较遗漏了 TeX 紧缩间距命令 `\!`。最终在公式等价断言中只去除空白和该间距命令，缓存一致性仍比较模型原文。[初次定位失败](../../tmp/h11-texo-chromium-20261119.log) · [间距断言记录](../../tmp/h11-texo-chromium-r2-20261119.log)

完整生产构建与 Node 缓存结构检查已通过。浏览器复跑指定 `VIBEFLOW_TEST_WEB_DIST` 和装有 Playwright 的 `VIBEFLOW_TEST_BROWSER_PYTHON`，增加 `--edge` 使用 Edge。模型和 WASM 均来自构建目录，无服务端推理依赖。[生产构建](../../tmp/h11-texo-build-20261119.log) · [缓存结构检查](../tools/texo-cache-smoke.mjs) · [界面检测](../../tmp/h11-texo-impeccable-20261119.json)
