# Claude Code API 配置与真实验收

2026-11-17：终端面板新增独立的“Claude Code API 配置”。在Agent面板保存本人的API密钥和默认模型，回到终端选择配置，点击 `claude ▸`，即可在当前算例目录启动已安装的Claude Code。不选择配置时沿用CLI原配置。切换选择后启动的是新终端，旧终端继续使用原绑定。

## 支持范围

- 当前平台绑定支持Windows cmd和本机实际安装的Claude Code；本轮版本2.1.218。WSL/沙箱绑定尚未接入，返回400并提示使用原CLI配置。
- Anthropic及kimi-coding提供方使用平台已有Messages地址规则。官方DeepSeek配置自动映射为 `https://api.deepseek.com/anthropic`，保持用户配置的模型名。仅精确匹配官方基础地址进行转换，不猜测自定义OpenAI网关的Messages地址。
- 自定义Messages服务可在平台选择Anthropic提供方并填写相应基础地址；纯OpenAI协议配置不展示在Claude下拉列表，后端也返回400。
- 仅导出当前用户自己的密钥。引用他人或共享配置返回403；启动放行前复查配置版本。已有终端不接受更换绑定。

DeepSeek映射依据[官方Claude Code接入说明](https://api-docs.deepseek.com/zh-cn/quick_start/agent_integrations/claude_code/)和[Anthropic兼容协议](https://api-docs.deepseek.com/guides/anthropic_api/)。私有配置目录使用Claude Code支持的 [CLAUDE_CONFIG_DIR](https://code.claude.com/docs/en/settings)。其他Anthropic服务、其他CLI版本未在本轮真实验收。

## 状态与凭据

复用原vault解密和终端生命周期，不改个人全局CLI配置。私有目录位于已有CLI状态根的 `users/<user>/windows/claude/providers/<profile>/<revision>`，同时设置 `CLAUDE_CONFIG_DIR`、HOME、USERPROFILE和临时目录。A→B→A回到同一配置版本时复用A的状态目录；修改密钥或模型后使用新版本目录。目录需随原私有CLI状态一起备份，API密钥备份仍依赖原provider数据库和vault密钥。

新进程清除继承的Claude/Anthropic路由与身份变量，保留Git Bash路径配置，再注入本次选择的地址、模型及 `ANTHROPIC_AUTH_TOKEN`。默认模型档位使用同一所选模型，避免继承服务账户的其他模型。密钥不放入命令行、浏览器、算例配置或NativeSession元数据。会话历史可能包含项目内容，仍属于原用户私有状态范围；这不是新增操作系统隔离。

项目/组织管理配置仍受Claude Code自己的优先级影响。平台的API选择不代表通用Agent协议、完整权限策略或所有Claude特性已经验收。

## 实际验收

入口 `server/live_llm_claude_provider_acceptance.py`；审查 `tmp/N7-05-claude-provider-review-20261117.json`，658份来源摘要及安装二进制摘要；结果 `tmp/live-claude-provider-uxdlpttk/claude-result.json`。

真实Chromium通过平台API、vault、终端和位于PATH之外、带空格及 `&` 的启动器执行本机Claude Code。隔离算例中放置随机proof文件，测试只允许实际Read工具，模型读取后回复随机值；没有子代理或模拟模型。测试通过回环观测器原样转发Messages请求和响应到用户授权的 `api.deepseek.com`，没有协议伪造或生成回复。

| 检查 | 结果 |
|---|---|
| 浏览器选择A→B→A | 3次新建会话，记录中的harness/profile与选择一致 |
| 实际服务/模型 | DeepSeek / deepseek-flash |
| 实际模型请求 | 6次 `/anthropic/v1/messages?beta=true`，全部HTTP200 |
| Read及回复 | 三轮均实际读取proof，最终成功回复；CLI均退出0 |
| 流式事件 | 实际Claude stream-json分别84、84、171条stream_event |
| 停止 | 3份持久关闭记录及根/gate实际退出均通过；API和Vite也已退出 |
| 凭据 | 浏览器命令、回复、私有CLI文件未发现明文密钥 |
| 拒绝行为 | 他人配置403，WSL绑定400，纯OpenAI协议400 |
| 界面 | 前端构建成功；Impeccable检测0发现；桌面1600×1000和手机390×844实际截图已查看，无页面水平溢出 |

Claude返回的三轮usage合计input_tokens=3460、cache_read_input_tokens=2432、output_tokens=384。它们是CLI报告的字段，未用CLI估算金额代替DeepSeek账单。模拟LLM调用为0；地址映射检查属于纯协议检查，不能替代真实模型证据。

本轮没有出现SQLite锁等待；此前OpenCode两次关闭记录保存失败仍保持未解决，见[已知问题](opencode-provider-profiles.md)。本轮通过不证明偶发问题已修复。完整深度协议、运行中取消/恢复、跨服务、WSL和其余CLI矩阵继续按FF-F57/J25及原主线验收。

主线48/53，剩5个大项；全部295项交付范围不变。本轮完成Claude配置接入增量及真实验证，不宣称整个多harness功能或全部部署矩阵完成。汇总证据：`tmp/N7-05-claude-provider-evidence/validation.json`。
