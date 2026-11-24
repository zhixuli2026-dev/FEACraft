# FF-F33 Ask 正文、推理与历史协议清洗

原解析器可清理 ASCII 伪工具标签和哨兵块。逐字符对照暴露两种漏滤的实际格式，即 DeepSeek V4 的全角 DSML 标签及 V3.1 的工具调用边界。更新沿用同一增量解析器处理模型正文和推理、会话详情、历史重发及原生 Codex 投影。普通相似标签和占位文本保留。已识别的未闭合块过滤到响应结束，解析器仅保留可能用于识别结束标记的尾部。 [解析器](../server/app/services/sanitizer.py) · [复现](../../tmp/FF-F33-sanitizer-20261121/protocol-baseline.json) · [DeepSeek V4 编码](https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash/raw/main/encoding/encoding_dsv4.py) · [DeepSeek V3.1 格式](https://huggingface.co/deepseek-ai/DeepSeek-V3.1#toolcall)

- 纯协议检查覆盖 13 个场景、1,436 个双块分割点、13 个逐字符流。对 128 KiB 已排除块检验中间内容的缓冲边界。 [实际验收入口](../server/live_llm_ask_sanitizer_acceptance.py)
- Chat Completions 与 Messages 均使用真实 DeepSeek。对照原始正文与推理、公开事件及持久化回复。把真实原始响应作为旧版历史记录写入隔离库，随后实际读取和对同一模型重发，确认出站助手历史与清洗后的文本一致。 [Chat Completions](../../tmp/live-f33-ask-sanitizer-ic8i140u/live-result.json) · [Messages](../../tmp/live-f33-ask-sanitizer-70v259xr/live-result.json)
- 两套正式 Ask 请求均可核对原始出站工具数量为 0。隔离观察器在专项中单独请求真实 DeepSeek 产生结构化写入。平台拒绝执行，历史没有该工具调用或待审批项，原文件字节不变。另复验 Windows 的实际 Codex Plan 问答、数据库脱敏和用量。 [Windows CLI](../../tmp/live-f30-codex-interaction-_b0x10ti/live-result.json)

本阶段 14 次真实 DeepSeek API HTTP 200，其中最终双协议通过组各 3 次。另有一个实际原生 CLI 回合上报用量。模拟 LLM 调用 0。早期模型选择解释标记，验收改为比较真实上游内容和平台清洗结果。Messages 一次验收曾把普通占位文字算作保留协议，精确比较后通过。历史模拟测试未执行。 [阶段汇总](../../tmp/FF-F33-sanitizer-20261121/validation.json)
