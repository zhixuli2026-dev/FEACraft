# FF-F56 Windows 沙箱部署与状态

在 Agent 工作台选择 Windows 后点击「检查运行环境」。对话框显示原生 Codex 的就绪状态和部署覆盖设置。状态为「尚未配置」或「需要更新」时，部署管理员在 Windows 主机上完成相应操作，再回工作台检查。WSL 显示自己的执行环境。可用模型目录支持展开，检查完成后会话自动关闭。

## 部署管理员操作

在 Windows API 的环境中设置 `VIBEFLOW_CODEX_WINDOWS_SANDBOX=unelevated` 或 `elevated`。省略该项时由安装的 Codex 选择默认模式。该设置在启动新的 Windows CLI 会话时生效。Codex 就绪状态适用于其当前配置，MXC 探测由 Windows/Codex 环境单独提供。 [Windows 沙箱文档](https://learn.chatgpt.com/docs/windows/windows-sandbox)

先在隔离部署目录中检查已安装的 Windows `codex.exe`。正式环境使用对应 Windows 服务身份及该账户或供应商配置实际使用的私有 `CODEX_HOME`。托管 API 凭据目录位于部署私有根的 `users/<id>/windows/providers/<profile>/<revision>/home`。确认该账户的相关会话已关闭后再初始化。 [部署工具](../scripts/configure-windows-codex-sandbox.py)

```powershell
& .\platform\server\.venv\Scripts\python.exe .\platform\scripts\configure-windows-codex-sandbox.py status --home $deploymentCodexHome --mode unelevated
& .\platform\server\.venv\Scripts\python.exe .\platform\scripts\configure-windows-codex-sandbox.py setup --home $deploymentCodexHome --mode unelevated
```

提权部署使用 `--mode elevated`，在启用 UAC 的测试环境验证授权或取消。工具等待 Codex 的完成通知，再检查就绪状态和进程退出。返回 `outcome_unconfirmed` 时先运行 `status` 核对实际状态。`setup_declined` 时可由操作员重新发起，`windows_logon_denied` 或 `windows_permission_denied` 时检查 Windows 服务身份与相关策略。 [原生 app-server 协议](https://learn.chatgpt.com/docs/app-server)

CLI 初始化写入私有配置的 `windows.sandbox`。托管启动只接受与部署覆盖一致的这一项额外配置，仍核对供应商配置和会话启动后的文件摘要。切换模式时，在停止该账户的会话后对同一私有目录重新初始化，并将部署设置改为对应模式。

## 实际验收

- Windows Codex `0.171.0-alpha.13` 在新私有目录查询返回 `notConfigured`，显式非提权初始化由原生 RPC 返回成功并复查为 `ready`。单独的提权状态查询返回 `updateRequired`。三次工具调用都核验退出。 [独立状态与初始化](../../tmp/ff-f56-final-tool-20261124)
- 真实 `command/exec` 在允许的工作区写入文件。工作区外和只读工作区的写入均以退出码 1 拒绝，目标文件未生成。捕获过程不请求模型，所属进程已退出。 [写入验证](../../tmp/ff-f56-native-cmd-rg5hyu8k/result.json)
- Edge 在 1440 和 390 px 显示实际 `ready` 和非提权设置，按 Esc 关闭。部署工具在该 API 测试账户的实际供应商私有目录完成初始化，核对原供应商配置各字段一致。随后的正式只读回合调用 DeepSeek `deepseek-flash` 两次，均为官方 `/responses` 流式 HTTP 200。Codex 通过实际 PowerShell 读取测试文件，最终回复与文件内标识一致，文件保留，检查会话、模型会话及隔离 API 核验退出。四轮累计实际 DeepSeek HTTP 200 共 8 次，模拟 0 次。 [最终结果](../../tmp/ff-f56-live-dos70zj1/windows-sandbox-live.json) · [窄屏截图](../../tmp/ff-f56-live-dos70zj1/windows-sandbox-workbench-390.png)

本机的 Windows UAC 已禁用，Windows Sandbox 客户端功能未启用。提权授权、取消后重新初始化以及提权模式下的隔离仍列入 T08。当前交付状态为实施中。 [主机状态](../../tmp/ff-f56-final-tool-20261124/host-policy.json) · [全功能计划](full-function-delivery-plan.md)
