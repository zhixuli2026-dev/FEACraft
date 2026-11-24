# Kimi 使用平台 API 配置

2026-11-17。Windows 终端中的 Kimi 启动按钮现在可以使用当前用户在平台保存的 API 配置。复用现有加密凭据库，不需要把密钥复制到命令或项目文件中。

## 使用和切换

1. 在 Agent 面板创建自己的 API 配置，设置密钥、基础地址和默认模型。
2. 打开终端，选择 Windows cmd，在“Kimi API 配置”中选择该配置；刚新增的配置可点击“重新读取 API 配置”。
3. 点击 `kimi ▸`。服务器读取并解密所选配置，将其传给新建终端进程。
4. 切换配置时重新选择，再点击启动按钮创建新会话。旧会话保留原环境，不会被页面的新选择改写。
5. 切回原配置，再次新建会话即可恢复使用该配置。选择“使用 CLI 原配置”恢复原快捷启动行为。

项目约定和文件备份继续由[CLI 契约与备份](cli-contracts.md)管理。这里切换的是 API 配置引用，不复制或备份明文密钥。删除或旋转 API 密钥后，无法从终端历史恢复旧密钥；应使用现有凭据库备份/恢复流程。运行中的 CLI 仍有启动时的凭据，密钥轮换时应关闭旧终端；当前功能不承诺自动撤销运行中进程的环境。

仅列出本人拥有、设置了密钥和默认模型的配置。交互式终端允许执行任意命令，能够读取自身环境，因此不会把他人的共享 API 密钥导出到该终端。无权使用配置时服务器返回403，不回退到服务账户凭据。

## 接口与部署

新建 `/ws/terminal` 可带 `cli_harness=kimi&provider_profile_id=<id>`，并使用 `shell=cmd`。未绑定保持原行为；缺少配置编号、其他harness或WSL绑定返回400。挂接已有session不能附带新的绑定参数，应新建会话。

服务端在释放终端执行前再次检查所选配置，启动期间密钥或模型变化返回409。终端列表/详情增加 `credential`，包含配置编号、提供方、模型、基础地址和版本摘要，无密钥。历史绑定随现有NativeSession保存，无数据库迁移。

Kimi 接收 `KIMI_MODEL_NAME/API_KEY/BASE_URL/PROVIDER_TYPE` 等环境变量，使用官方[临时模型配置机制](https://www.kimi.com/code/docs/en/kimi-code-cli/configuration/env-vars)。当前上下文长度设为131072，不声明图片或推理扩展能力；模型高级参数仍待完整配置矩阵。

Kimi 状态保存在既有私有CLI根的 `users/<user>/windows/kimi/providers/<profile>/<revision>` 下。不同配置和密钥版本使用不同目录；同一配置重启可访问其Kimi历史。部署备份应包含该私有根、平台数据库和vault密钥，详见[凭据库持久化](provider-vault-persistence.md)。原服务账户Kimi目录未修改。CLI私有状态容量和保留策略继续属于原资源交付范围。

本记录验收Windows Kimi；后续新增的OpenCode 2接入及SQLite关闭问题见[OpenCode配置记录](opencode-provider-profiles.md)。Claude及通用终端Codex的API配置接入、WSL、各CLI深度协议、取消和恢复矩阵继续FF-F57/J25；独立Codex原生会话已存在的凭据接入仍见[托管CLI凭据](hosted-cli-credentials.md)。不将本次Kimi验证视为其他CLI完成。

## 实际验收

显式入口 `server/live_llm_cli_launcher_acceptance.py --provider`，源码审查 `tmp/N7-05-kimi-provider-review-20261117.json`，655份源码摘要。沿用已审查的真实API、加密vault、文件、PTY、进程停止和Chromium工具，不运行历史模拟回归集合。

`tmp/live-kimi-provider-hj_ub8f5/launcher-result.json` 整轮passed=true。测试API没有继承KIMI_MODEL凭据；先通过真实API创建A/B两份配置，再由真实浏览器选择A→B→A，启动安装的Kimi 2.6.2，每轮真实Read读取未知文件内容并由DeepSeek回复。两份配置使用同一授权DeepSeek服务和模型，验证配置选择与独立状态，不声明跨提供方或跨模型兼容性。

- 真实模型：`api.deepseek.com / deepseek-flash`，6次HTTP200，三轮合计1次CLI超时重试，模拟0。调用数统计收到的HTTP响应；未采集token用量。
- 实际基础设施：3个终端通过界面关闭，持久停止记录与实际进程退出均核验；API/Vite已退出；Kimi状态文件未保存明文密钥。
- 实际拒绝请求：另一账户引用配置403；WSL或Claude请求该绑定400。未用模拟故障或固定模型结果验证。
- 前端构建通过；机械检测无发现；查看1600×1000和390×844截图，手机宽度无水平溢出。截图检查后将提示中的“设置页”纠正为实际入口“Agent 面板”，该文字修正不重复付费模型调用。

主线48/53，剩5个大项；N7-05保留原3组退出条件。295项全功能目标保留，严格整项关闭仍为0；已有实现和分项证据不为0。下一步接入剩余CLI配置，继续原始验收条件。
