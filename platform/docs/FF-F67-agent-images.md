# FF-F67 Agent 图片和历史回读

2026-11-24 在隔离算例用 Edge 和用户已授权的 `api.deepseek.com` / `deepseek-flash` 实测上传和投递。选择两张 PNG 后移除其中一张，模型从剩余图片准确识别一个从未出现在文字提示里的随机编号。刷新后，浏览器通过有权限检查的文件接口读取原始图片并显示 Blob，尺寸及 SHA-256 一致。第二回合没有新增图片。发送到真实模型的两次请求均带同一张图片的原始字节，收到 2 次 HTTP 200，模拟 0 次。 [实际回执](../../tmp/live-f67-agent-image-49kmoh4b/image-result.json) · [历史截图](../../tmp/live-f67-agent-image-49kmoh4b/visible-image-history.png) · [DeepSeek 图片协议](https://api-docs.deepseek.com/guides/vision/)

图片事件保留文件引用、显示名、MIME 和大小。无逗号的 Data URL 原先会在解包时异常，现返回 400。含一张合法图和一张非法图的批次中，格式错误、MIME 不符与 Base64 无效均在写入前拒绝。另一账号访问图片原文件和会话均为 404。验收结束后隔离 API 已退出，端口已关闭，模型密钥未写盘。 [验收入口](../server/live_llm_agent_image_acceptance.py) · [原生路径与 S3 测试](native-cli-images.md)

模型窗口上线后重新验证同一图片链路。新图片生成的随机编号准确读出，原始字节与历史第二回合的外发字节一致。新增 2 次实际 DeepSeek HTTP 200。 [窗口变更后回归](../../tmp/live-f67-agent-image-cho285xh/image-result.json)
