# FF-G12 Agent 语音输入

用户点击「语音输入」后浏览器请求麦克风权限。转写仅追加到当前 Agent 草稿，由用户审核后发送。切换算例或会话、离开面板会停止录音并忽略迟到结果。录音及转写期间可取消。拒绝授权、无法访问设备、静音、服务忙或转写失败都有可重新操作的提示。 [Agent 面板](../web/src/panels/ChatPanel.tsx) · [录音实现](../web/src/utils/voiceInput.ts)

部署配置 `VIBEFLOW_VOICE_TRANSCRIBER_PATH` 和 `VIBEFLOW_VOICE_MODEL_PATH` 后，浏览器在停止时将最多 30 秒的 16 kHz 单声道录音送至平台服务器。该服务器使用本地 Whisper CLI 转写。接口检查算例编辑权限、格式和大小，限制并行工作并给出超时，完成或取消后删除临时文件。两项未配置时沿用浏览器原生语音识别，实际可用性由浏览器环境决定。 [转写接口](../server/app/routers/voice.py) · [whisper.cpp CPU 发行包](https://github.com/ggml-org/whisper.cpp/releases/tag/v1.8.5)

## 实测

- Windows 平台上的真实 Whisper CPU 进程分别输出「管道內的壓力是均勻的」和 “The pressure inside the pipe is uniform.”。拒绝未认证、无算例授权、损坏格式、静音和超长录音。故意替换为损坏的权重文件时返回 502，恢复原文件即可转写。客户端断连后对应实际进程退出，临时目录清空，下一次转写成功。最终验收离线转写成功 5 次，聊天 LLM 调用和模拟次数均为 0。 [服务结果](../../tmp/ff-g12-voice-s_6kkytz/voice-actual.json) · [进程回执](../../tmp/ff-g12-voice-s_6kkytz/voice-process.jsonl)
- Edge 使用 Windows 合成的测试音频作为媒体设备输入，走实际 MediaRecorder、WAV 上传和离线识别。麦克风权限被拒绝时没有上传，授权后草稿保留既有文字并追加真实转写结果。重新加载时停止录音且没有产生新请求。1440px 和 390px 均可操作。本轮未采集物理麦克风。 [浏览器](../../tmp/ff-g12-voice-s_6kkytz/voice-browser.json) · [桌面](../../tmp/ff-g12-voice-s_6kkytz/voice-desktop.png) · [手机](../../tmp/ff-g12-voice-s_6kkytz/voice-mobile.png)
- 当前 Edge 报告本地浏览器识别包不可用，原生识别探测返回采集错误。离线服务使用多语言 `ggml-base-q5_1.bin`。上游二进制 CDN 握手失败后，从微软公开源码中的三个分片恢复，合并文件的长度和 SHA-256 与上游发布值完全相符。浏览器生产构建通过，现行部署使用原有配置。 [浏览器引擎探测](../../tmp/ff-g12-edge-native/native-capability.json) · [采集错误](../../tmp/ff-g12-edge-native/native-audio-result.json) · [模型来源和摘要](../../tmp/ff-g12-whisper-model/model-source.json) · [上游权重](https://huggingface.co/ggerganov/whisper.cpp/blob/main/ggml-base-q5_1.bin)
