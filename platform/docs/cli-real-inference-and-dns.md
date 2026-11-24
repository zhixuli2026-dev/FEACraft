# WSL CLI 真实模型与工具回合验收

2026-11-08。此前 [会话范围恢复](cli-managed-session-recovery.md)中的模型证据来自平台 Agent；本阶段改由安装的 Linux Codex CLI 经正式 session/turn 路由直接请求用户授权的 DeepSeek，验证 CLI 自身的推理和工具循环。

## 协议与连接

[OpenAI 官方配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)规定自定义 provider 使用 Responses；[官方网关要求](https://learn.chatgpt.com/docs/enterprise/gateway-compatibility)也明确：Chat Completions 成功不能证明 Responses、工具循环或流式终态成功。

本次对 `api.deepseek.com` 实际发出的 `/v1/responses` 和 `/v1/chat/completions` 均返回 HTTP 200、`deepseek-flash` 和正确随机字符串，分别报告 90、88 tokens。[直接协议证据](../../tmp/live-cli-protocol-0ay_u4yt/live-result.json)保存了时间、响应 ID、用量和内容。因此没有实现或使用协议转换器，更没有模拟 Responses 输出。

真实 CLI 首次运行持续网络重连、120 秒后中断。随后从 WSL 的 root 和 nobody 身份实测到 DNS 解析失败或耗时约 12 秒。隔离 mount namespace 中替换 resolver 后，当前 WLAN DNS `192.168.1.1` 的解析约 11 毫秒，HTTPS 到达服务并返回预期的未认证 401；这一步只证明网络，不计为模型推理成功。

临时替换 `/etc/resolv.conf` 会被 WSL 自动恢复成 `/mnt/wsl/resolv.conf` 链接，真实 CLI namespace 仍读到旧网关 `192.168.176.1`。最终新增发行版内 `/etc/wsl.conf` 的 `[network] generateResolvConf=false`，保留旧配置备份，再仅重启 `vibeflow-linux` 激活。[Microsoft 的配置说明](https://learn.microsoft.com/en-us/windows/wsl/wsl-config)要求停止并重新启动发行版才能应用配置。

重启前已核验没有测试 CLI、求解任务或用户 shell，只剩 WSL init 和 Docker 集成代理；没有执行全局 `wsl --shutdown`，没有修改共享 `/mnt/wsl/resolv.conf`。原始内容备份在发行版内 `/etc/resolv.conf.vibeflow-before-20261108-cli-inference`，原链接目标及操作前进程清单见 [激活证据](../../tmp/cli-inference-dns-activation.json)。当前 resolver 来自本机 WLAN 配置，换网络后需重新验证；部署环境不应照抄这个地址。

回滚时先核对两份配置仍为本轮版本，再恢复原 `/etc/resolv.conf` 链接并撤销本轮新增的 network 配置；只重启指定发行版使其生效，不能覆盖后续管理员改动。

## 已通过的真实 CLI 场景

显式入口 `platform/server/live_llm_cli_inference_acceptance.py --source-profile-id 26`；`--read-file` 验证实际工具循环，`--protocol-probe` 为直接服务协议核验，两者互斥。凭据只读自本地授权配置，写入隔离加密配置；没有写开发库或输出密钥。

- [文字回合](../../tmp/live-cli-inference-6tiwmper/live-result.json)：CLI 实际接收流式文本和 `turn/completed`，答案与随机输入完全一致；平台保存消息、关闭 session、提交 completed 回合并释放执行锁。CLI 报告 **8,708 tokens**。
- [工具回合](../../tmp/live-cli-inference-wk5zhgcj/live-result.json)：提示词不含答案，模型选择 `/usr/bin/bash -c 'cat Ref/cli-inference-proof.txt'`，真实命令以退出码 0 返回文件内随机值；模型再次回答，文本与工具输出及持久消息一致。CLI 报告 **19,970 tokens**。此运行未产生待用户批准的请求，不能据此宣称审批回复路径已验收。

两者均使用真实安装的 CLI、实际 native scope 和正式 API。记录中的 `upstream_success_count_verified=false` 表示没有对 CLI 内部 HTTP 请求逐次计数；不把一个 CLI 任务回合简单记作一次模型请求。CLI 用量是其实际 tokenUsage 通知，不是自行估算。直接服务请求和平台 Agent 请求的 HTTP 日志单独统计。

## 真实失败与收尾延迟

- [第一次 DNS 失败](../../tmp/live-cli-inference-nk5qiwyq/live-result.json)、[临时配置被自动恢复](../../tmp/live-cli-inference-26qnjts8/live-result.json)：CLI 请求未取得模型成功证据，按期限中断，实际 scope 已清理。
- [库存巡检取消](../../tmp/live-cli-inference-4qz1puot/live-result.json)：`CaseWriteConflict` 导致关闭，回合未开始推理。原日志只保存异常类型，具体库存失败原因未确证；后续成功不等于该问题已修复。
- [首次工具场景配置不当](../../tmp/live-cli-inference-8sfl_hze/live-result.json)：真实模型已选择读取命令，但测试采用 ask 模式且没有批准请求，最终超时，审批过期、scope 清理。该次失败保留。后续工具场景使用 approve_writes；测试若收到审批，只允许精确匹配隔离目录里这条 cat 命令的单次 accept，不批准其他命令或会话级权限。

文字场景的模型终态到平台完成约 54 秒；工具场景的实际计时中，session 收尾约 42.8 秒，初始化结算约 17.2 秒，随后还排空了一轮用户库存扫描。代码中 `observe_storage` 在节点扫描结束后无条件启动用户扫描，即使 session 已进入 stopping。已补充 stopping 检查：保留正在进行的节点扫描排空，关闭中不再启动下一轮用户扫描。新的实际验收结果记录于本阶段 validation；该改动不等于全部库存慢扫描或间歇拒绝已经解决。

[修复后的真实工具回合](../../tmp/live-cli-inference-at0esfvr/live-result.json)通过，报告 **19,966 tokens**；关闭后新增用户扫描为 0，session 收尾约 **14.9 秒**，初始化结算仍约 **11.2 秒**。这是一对实际运行的观测结果，不是稳定吞吐量基准。三个成功 CLI 回合共报告 **48,644 tokens**；另有两次直接协议请求共 178 tokens，统计不包含失败场景中未取得完整用量报告的模型尝试。

## 后续与交付范围

真实文字和读取工具循环已有证据；Windows 原生模型回合、真实审批回复/拒绝/取消、fork/resume、跨 API 退出后的执行锁/turn/审批/staging 恢复仍待验收。范围恢复不能制造模型完成结果。库存间歇取消、未终止扫描时长、guardian 证据峰值预留与全局预算继续处理。

主线 **33/53 完成，剩 20**；资源 **11/17，剩 6**；全功能 **295 项仍待整项交付验收**。新增真实模型证据增强了交付基础，尚不满足整项功能的全部验收条件。
