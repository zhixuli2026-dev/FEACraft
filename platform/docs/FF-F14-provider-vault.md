# FF-F14 模型端点与凭据保护

模型基础地址必须使用 HTTPS 或显式回环 HTTP。服务端拒绝用户信息、查询参数、片段和无效端口，公开配置只返回密钥存在状态。模型传输、连接测试及翻译错误应用统一脱敏，覆盖 Bearer、JSON 密钥字段、实际凭据和 URL 编码。 [模型配置校验](../server/app/routers/llm.py) · [错误脱敏](../server/app/services/redaction.py)

- Windows 本地新建 vault 用当前进程账号的 DPAPI 保护 Fernet 主密钥，主密钥只在完成写入后原子发布。原有 Fernet 文件可读，显式配置的部署密钥按原路径验证和读取。损坏的文件保留以供恢复。 [Vault 实现](../server/app/services/provider_vault.py) · [Microsoft DPAPI 跨进程与账号说明](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata)
- 在隔离的 Windows API 保存实际 DeepSeek 配置，验证 DPAPI 文件，关闭并重启 API。读回原配置后实际翻译成功。16 个并发初始化调用共享一个主密钥，旧格式读回、损坏 DPAPI 拒绝及错误脱敏通过。前两次预检发现 Windows 启动器退出早于实际 API，均在推理前终止。最终核对两次实际进程退出。 [真实验证](../../tmp/live-f14-provider-vault-ya74vlaf/vault-result.json) · [验收入口](../server/live_llm_provider_vault_acceptance.py)
- WSL 启动环境和启动参数只有允许的路径和运行信息。密钥在所属进程的最终放行阶段通过私有 stdio 交给固定桥，再放入 CLI 子进程的专用环境。实际非特权 WSL 程序验证凭据存在于最终子进程环境，WSLENV 和启动描述均无凭据，执行范围已清空。改动后 Windows、WSL 原生 Codex 分别取得官方 DeepSeek HTTP 200。 [WSL 环境过滤](../server/app/services/codex_environment.py) · [启动管道](../server/app/services/owned_stdio.py) · [WSL 传输回执](../../tmp/FF-F14-vault-20261124/wsl-secret-transport.json) · [双环境真实模型回执](../../tmp/live-f10-native-reply-sound-atbkjene/native-reply-sound-result.json)

本轮共 3 次真实 `deepseek-flash` HTTP 200，包括一次重启后的翻译和两个实际原生回合，模拟 0。既有部署密钥继续按原身份与恢复流程使用。 [部署凭据与迁移](hosted-agent-auth.md)
