# FF-E16 PDF 划词翻译与复制

在 PDF 文本层选择内容后，查看器提供「翻译选中文本」。带汉字的选区译为英文，其余选区默认译为简体中文。结果卡显示翻译进度、译文或可重试的错误。复制按钮只在收到有效译文后显示，实际写入剪贴板后给出反馈。[查看器](../web/src/panels/ViewerPanel.tsx) · [翻译 API](../server/app/routers/llm.py)

翻页、缩放、切换算例或打开其他 PDF 会清除当前选区并让旧响应失效。翻译卡的鼠标操作独立于 PDF 文本层。服务端在系统要求和请求正文中明确指定目标语言，拒绝空结果和原样回传的中译英内容。显式选择模型配置仍须通过访问检查。

## 浏览器和真实模型验收

用 WSL 非特权用户和实际 XeLaTeX 编译两页双语 PDF，经公开文件上传接口写入隔离算例。真实 Edge 打开文件树中的 PDF，通过 PDF 文本层用鼠标拖选两段文字。缺省模型配置时实际返回 400，补配置后在原结果卡重试成功。英译中和中译英的译文均由 `api.deepseek.com` 的 `deepseek-flash` 生成，点击复制后通过浏览器剪贴板读回并与实际 API 译文逐字比对。匿名调用返回 401，异账户指定他人配置返回 404。[最终回执](../../tmp/live-pdf-translation-l5qhsshk/live-pdf-translation-result.json) · [英译中](../../tmp/live-pdf-translation-l5qhsshk/translation-zh.png) · [中译英](../../tmp/live-pdf-translation-l5qhsshk/translation-en.png) · [缺配置提示](../../tmp/live-pdf-translation-l5qhsshk/translation-error.png)

另一条请求的真实 DeepSeek 响应已到达 API。验收短暂延迟交付，Edge 翻到第二页并选择另一段文字后，前一页的 HTTP 200 响应才送达。当前选区仍能翻译，旧结果未进入当前卡片。最终三次实际 HTTP 200 合计 401 tokens。隔离 API 已退出，验收用加密配置的解密密钥只存在于该进程内存。模型模拟调用为 0。[上游回执](../../tmp/live-pdf-translation-l5qhsshk/upstream.jsonl) · [显式验收入口](../server/live_llm_pdf_translation_acceptance.py)

旧版本对照复现了缺配置时仍显示「复制译文」。首轮真实验收另有三次 HTTP 200，其中一次把中文原样返回，未通过语言检查。两轮实际 DeepSeek 请求合计六次 HTTP 200、777 tokens。起初并行开发期间，夹具从 LaTeX 产品入口制样遇到暂时的处理器导入错误，尚未调用上游。对照及最终复验均改用独立 XeLaTeX 制样和公开导入，保留了原始失败证据。[旧版浏览器对照](../../tmp/live-pdf-translation-f36hnlmf/live-pdf-translation-result.json) · [首轮模型回执](../../tmp/live-pdf-translation-k12f514m/upstream.jsonl) · [原样回传画面](../../tmp/live-pdf-translation-k12f514m/browser-failure.png) · [首次制样记录](../../tmp/live-pdf-translation-s2cxuqd8/live-pdf-translation-result.json)
