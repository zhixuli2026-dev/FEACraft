# FF-E15 PDF 阅读器

查看器通过认证 HEAD 核对文档版本和长度，按当前页所需字节请求 HTTP 206。换页、缩放、旋转都会取消前一次绘制和文字流。文档切换会取消旧文档的字节请求。像素画布以最多 1,600 万像素和单边 8,192 像素限制分配，保留完整的页面布局和文本选择坐标。[查看器](../web/src/panels/ViewerPanel.tsx) · [Range](../web/src/utils/pdfRange.ts)

全文搜索由实际 PDF.js worker 逐页提取文字，支持取消、空白和大小写归一、多文本段共同命中及同页多处定位。搜索结果以 50 页分批展示；选择匹配页会将第一处高亮滚入视口。高亮使用原文本层的 DOM Range 坐标，不接管选择事件。目录和页码输入均能跳页，页码、缩放及旋转按文档版本恢复。[文本搜索](../web/src/utils/pdfTextSearch.ts) · [读页状态](../web/src/utils/viewSelection.ts)

Edge 157 用正式分块上传入口打开 56 页、64,547,859 字节的 PDF。首屏加载了 4,779,027 字节（7.4%），所有正文请求均为 206，版本与 HEAD 一致。本次首屏约 5.0 秒，包含工作台交互及浏览器渲染。搜索能取得 56 页结果，第 56 页上跨字体段的短语命中两次。对原页旋转 90° 及用户再旋转 90° 两种状态，实际高亮落在文本层、覆盖画布字形并保留可选文本。[完整回执](../../tmp/pdf-reader-e15-dvfjpjj8/pdf-reader-result.json) · [原页旋转](../../tmp/pdf-reader-e15-dvfjpjj8/pdf-native-rotation-search.png) · [用户旋转](../../tmp/pdf-reader-e15-dvfjpjj8/pdf-added-rotation-search.png)

验收对真实字节区间人为延迟一次，观察搜索取消后无残留结果，并核对换文档时网络返回 `net::ERR_ABORTED`。另验证跳页、目录、缩放、刷新后的状态恢复，跨账户和未登录访问分别为 404 和 401。第一次对照曾在浏览器退出后注册另一账户时超时。验收脚本调整为在浏览器启动前核对鉴权后，新版与旧包对照均退出 0。旧包可加载同一实际 PDF，页码输入、手动旋转和高亮均在本轮新增。模型调用为零。[验收脚本](../server/pdf_reader_browser_acceptance.py) · [旧包对照回执](../../tmp/pdf-reader-e15-0qn432p4/pdf-reader-result.json)
