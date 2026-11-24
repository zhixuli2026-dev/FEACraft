# FF-F24 与 FF-F25 Agent 命令运行环境

Agent 的 `run_shell_command` 保留 Windows 默认命令解释器、显式 PowerShell 和 WSL 三种执行方式。WSL 通过发行版内的 `nobody` 运行，保持算例工作目录和 Bash 语义。PowerShell 保留 UTF-8 stdout、文件读取、退出码和原始命令，常规非交互进度提示从工具输出中移除。 [运行入口](../server/app/services/agent_shell.py) · [Windows 启动器](../server/app/services/agent_shell_native.py)

- 本机 WSL 的发行版默认账号实际返回 UID 0。修复后，真实 DeepSeek 选择的 WSL 命令从隔离算例读回随机标识并返回 UID 65534。只有测试请求中的精确命令、运行环境和时间上限匹配时才批准。Windows 原生命令和 PowerShell 也读回同一文件，PowerShell 的中文 stdout 保持完整。 [真实回归](../../tmp/live-f24-f25-agent-shell-3swpk2af/live-result.json)
- 实际 WSL 命令到达原生放行前时取消，边界文件未创建。另一命令创建运行标识后被取消，退出凭据确认所属范围为空。同一会话随后再次执行获批命令，以 UID 65534 正常读回文件。重复批准返回 409，外账号读取执行记录返回 404。 [真实入口](../server/live_llm_agent_shell_runtimes_acceptance.py)
- 此前独立 PowerShell 实测以真实模型选择工具。准备和运行共享期限，到期停止，已核验进程退出，模型据实返回超时。 [实际期限回归](../../tmp/live-shell-deadline-mtju5vew/live-result.json)

本轮最终通过 10 次真实 `deepseek-flash` HTTP 200，模拟 0。首轮 7 次真实请求已通过三种解释器和 WSL 运行中取消，并保留修复前 PowerShell 的 CLIXML 进度提示。扩展复验通过后保留上述结果作为记录。放行前检查只暂停隔离验收中的实际启动门。 [首轮证据](../../tmp/live-f24-f25-agent-shell-jbpx5prd/live-result.json)
