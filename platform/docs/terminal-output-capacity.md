# 终端输出缓冲与慢连接

## 2026-11-19 普通 cmd 和 WSL 的运行预算

普通终端可设置 `VIBEFLOW_TERMINAL_SESSION_LIFETIME_SECONDS` 和 `VIBEFLOW_TERMINAL_SESSION_MAX_OUTPUT_BYTES`。两项默认均为 `0`，保留默认终端的运行和重连行为。启用的限额在会话启动时确定，浏览器断开或重新挂接时继续使用同一额度。累计输出按 PTY 原始字节计算，独立于每个连接的积压和会话历史尾部。[服务配置](../server/.env.example) · [终端实现](../server/app/services/terminal_sessions.py)

达到任一配置限额时，服务以 `timeout` 或 `log_limit` 停止原进程范围，核验退出并持久保存错误和停止回执。完成清理后从实际会话列表移除。重新启动 API 后查询和回放仍提供相同的回执。普通终端磁盘预算和跨 API 的终端名额继续按原计划验收。[WebSocket 状态](../server/app/routers/terminal.py)

四种部署共 24 个真实会话。8 个默认会话在启动后降低配置仍跨越 64 KiB 和新增时间门限，断线重连后继续运行。8 个限时及 8 个累计输出受限的会话实际到达门限，核对原进程和派生进程停止。设为 9 秒的限时会话从就绪到收到已核验停止结果为 9.641 至 11.407 秒。各组冷启动复查历史和回放。该组真实及模拟模型调用均为 0。[SQLite 与文件系统](../../tmp/terminal-running-budget-xricpbqz/terminal-running-budget.json) · [PostgreSQL 与文件系统](../../tmp/terminal-running-budget-lul3yc00/terminal-running-budget.json) · [SQLite 与 S3](../../tmp/terminal-running-budget-56xlrdmp/terminal-running-budget.json) · [PostgreSQL 与 S3](../../tmp/terminal-running-budget-qd6elg0z/terminal-running-budget.json) · [复跑入口](../server/live_llm_terminal_running_budget_acceptance.py)

## 2026-11-17 连接缓冲

2026-11-17，N4-05 / RB-14 的具体缺口。主线仍为 49/53、剩 4 项；资源清单仍为 11/17、剩 6 块。本项不代表全部原生入口、线程池或全局并发已完成。

## 实际复现

原终端为每个浏览器订阅创建无界队列，历史缓冲只按帧数截断。通过真实 cmd 和非特权 WSL 输出 4 MiB，在隔离 API 中暂停一个 WebSocket 的实际发送，另一连接正常接收：慢连接积压约 4 MiB，历史缓冲也约 4 MiB。原记录 `tmp/terminal-output-capacity-_b2xrk0s/output-result.json` 明确 `passed=false`、`reproduced=true`。

原 PTY 已实际关闭，停止凭据和独立进程观察保留。首次 API 启动器退出后立即观察到实际 API 仍存活；随后独立核验其出生身份退出，补充在 `independent-api-exit.json`。后续验收改用实际 Uvicorn 正常关闭并断言原 API 已退出，不把启动器退出当成 API 退出。

## 实现

- 每条终端连接最多 1 MiB UTF-8 输出、256 项；入队前检查，超限释放积压并唤醒连接处理器。
- 历史缓冲最多 256 KiB、1024 段，与原持久尾部大小一致，保留合法 UTF-8。初次重连快照也纳入转发任务的 finally 清理。
- 二进制输出及最终状态发送共用单次 10 秒期限；超限/发送超时后返回 `terminal_output_limit`，以 WebSocket 1013 关闭该连接。提示发送与关闭各有 2 秒期限。
- 慢连接不会关闭 PTY，也不会重放输入命令。其他正常连接继续接收；用户可重新挂接原会话，读取最近输出并继续输入。
- 使用 `asyncio.timeout` 直接等待发送。最初逐帧 `wait_for` 创建额外任务，实际突发输出使正常连接也积压并被关闭；该失败修正后保持原限额，没有通过扩大缓冲隐藏问题。

## 实际验收

入口 `platform/server/terminal_output_capacity_acceptance.py --require-bounded`，最终审查 `tmp/N4-05-terminal-output-review-v4-20261117.json` 覆盖 644 份生产及导入辅助源码。输出来自实际 Python 命令和真实 PTY，包含中文及 emoji；测试只暂停真实 WebSocket 发送并观察真实队列入队/出队，不替换输出内容、进程或模型。

最终证据：`tmp/terminal-output-capacity-maz_admy/output-result.json`。

| 实际 shell | 慢连接最大排队字节 | 正常连接最大排队字节 | 正常收到字节 | 历史缓冲字节 |
| --- | ---: | ---: | ---: | ---: |
| Windows cmd | 1,000,063 | 16,384 | 4,195,153 | 258,235 |
| WSL UID 65534 | 1,031,910 | 98,633 | 4,194,630 | 258,583 |

两类 shell 的慢连接均收到明确错误和 1013 关闭；正常连接收到实际完成标记和全部输出。原会话重连后真实写文件通过，另一次只阻塞历史快照、没有大量新输出的连接在约 10 秒后释放。最终原会话 closed、停止凭据 verified；5 个记录的终端进程出生身份及实际 API 均已退出。

中间失败 `tmp/terminal-output-capacity-dsxvj8wf` 保留正常连接未收到标记的结果；补充观测后的 `tmp/terminal-output-capacity-z39229qz` 明确记录正常连接仅收到 512,662 字节即被误断开。原始与中间实现快照均保留。最终通过没有覆盖失败记录。

本轮模型调用 **0**、模拟模型调用 **0**、Claude 调用 **0**，属于实际命令、网络流控与资源行为验收。浏览器已有的 `TerminalOutputQueue` 使用独立 2 Mi 字符上限，本轮未修改前端。其他入口的有界队列、整体连接数量、跨节点全局额度和 OS 目录外强制范围继续按原 RB-12 至 RB-17 推进。
