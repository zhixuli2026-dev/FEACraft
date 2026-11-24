# Codex 终端 API 配置

2026-11-17：终端面板新增“Codex Responses API 配置”。在Agent面板保存本人API密钥、基础地址和默认模型，然后在终端选择配置并点击 `codex ▸`。不选择时沿用CLI原配置。选择仅作用于新终端，已有会话不会更换凭据。

## 协议和配置

该入口要求服务支持Responses API，不将Chat Completions转换为Responses。Messages提供方不显示在Codex选项中，后端也返回400。自定义OpenAI兼容提供方仍需管理员确认其实际支持Responses；界面标签与说明明确这一条件。普通WSL/沙箱API绑定尚未接入，返回400；原CLI配置入口仍可使用。

复用 `codex_credentials.endpoint/config/overrides`，设置自定义 `vibeflow_api` 提供方、`wire_api=responses`、环境变量凭据及关闭WebSocket传输，符合[官方配置参考](https://developers.openai.com/codex/config-reference/)。仅官方DeepSeek基础地址自动归一为 `https://api.deepseek.com`，对应其[官方Responses接口](https://api-docs.deepseek.com/guides/responses_api/)。自定义地址保持原值，不猜测端点能力。

私有CODEX_HOME位于已有CLI根的 `users/<user>/windows/codex/providers/<profile>/<revision>`。平台在每次绑定启动时原子生成该目录的 `config.toml`，包括模型和提供方，不含明文密钥；这里的配置由平台管理，不用于手工维护CLI设置。会话历史保留，切回相同配置版本复用目录，配置/密钥版本改变后使用新目录。原服务账户个人Codex目录不被修改。

复用原vault解密，密钥只通过 `VIBEFLOW_CODEX_PROVIDER_TOKEN` 传入进程，生成的shell环境策略排除该变量。交互终端本身能够读取自己的环境，因此只允许本人配置，不导出共享密钥。启动前复查原配置；他人配置403，已有会话不能更换绑定。平台没有新增权限审批或隔离设计。

Codex子进程显式设置回环地址NO_PROXY，避免Windows系统代理截获所选本地网关；保留原NO_PROXY条目。公网连接仍遵从当前运行环境网络配置。本轮只修改终端入口；后续核对发现原生app-server路径已有回环免代理，并已通过[真实原生会话流程验收](codex-native-responses-lifecycle.md)，无需重复修改。

## 实际验收

入口 `server/live_llm_codex_terminal_provider_acceptance.py`，浏览器驱动 `tools/live-cli-launcher-browser.cjs`；最终审查 `tmp/N7-05-codex-terminal-provider-review-v2-20261117.json`，659份来源及本机Codex二进制摘要。

| 轮次 | 实际结果 |
|---|---|
| `tmp/live-codex-provider-0g_z9ga9` | CLI请求返回502并退出1，观测转发器未收到请求，0次真实模型成功响应；整轮失败保留 |
| `tmp/live-codex-provider-2cgbdg04` | 显式回环免代理后，浏览器A→B→A三次新终端全部通过，6次真实DeepSeek Responses HTTP200 |

运行版本 `codex-cli 0.167.0-alpha.4.1`，服务 `api.deepseek.com`，模型 `deepseek-flash`。实际Codex通过shell工具执行PowerShell `Get-Content`读取隔离算例随机proof，再由模型回复该值；文件内容保持不变，CLI三次退出0，三份NativeSession关闭记录及根/gate实际退出均验证。API、Vite和首轮失败遗留的实际进程也均已核验退出；未把首轮数据库中的未完成状态改写为成功。

测试使用实际平台API、vault、PTY和浏览器，启动器位于PATH之外且路径带空格及 `&`。回环观测器把原请求和响应转发到真实 `/responses`，不生成回复、不转换协议；最多24次真实请求。本轮模型6次HTTP200，模拟0。测试关闭Codex多代理与网页搜索，在用户已授权的本地完整权限下只读测试文件。浏览器现在遇到真实CLI非零退出标记会立即保存失败，不再继续等成功标记。

CLI报告三轮usage合计input_tokens=45726、cached_input_tokens=35968、output_tokens=419、reasoning_output_tokens=29。缓存与推理字段按原值记录，不重复加总为总用量，不用CLI估算替代实际服务账单。

前端 `tsc -b && vite build`通过，Impeccable检测0发现；1600×1000桌面及390×844手机截图实际查看，无页面水平溢出。配置切换、完成后关闭已验证；模型运行中取消、历史恢复、跨服务、其他系统及完整Agent协议矩阵尚未由这轮证明。

## 保留的问题与后续

- 当前Codex目录没有 `deepseek-flash` 元数据。三轮均真实输出“Model metadata … not found … fallback metadata”提示，同时工具执行及 `turn.completed` 成功。没有伪造目录或隐藏提示；上下文、能力目录和完整模型选项继续验收。
- 旧OpenCode两轮SQLite关闭记录保存失败仍未定位。本轮没有出现锁错误，不代表旧问题已经修复。
- Kimi、OpenCode 2、Claude Code和Codex现均有Windows终端API选择及各自真实验收证据；这不等于四种CLI深度协议、所有权限/取消/恢复组合已完成。

主线48/53，剩5项，N7-05原3组条件不变；全部295项交付目标继续。汇总证据：`tmp/N7-05-codex-terminal-provider-evidence/validation.json`。
