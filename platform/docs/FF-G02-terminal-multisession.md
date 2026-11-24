# FF-G02 多本地终端会话

2026-11-19 完成验收。工作台在算例下管理多个有独立标识的原生终端。切换或断开浏览器订阅后，实际进程继续运行。返回原会话会回放断开期间保存的输出，并恢复输入。显式关闭等待真实进程和启动门退出，沙箱输出发布成功后可从历史回看。重复关闭返回已完成的结果。

第二个 API 实例可以从统一活动历史读取持久记录。会话控制仍归创建它的 API 实例。浏览器在第二个实例尝试关闭会收到归属拒绝，活跃会话也会阻止复制算例。普通终端的空闲回收期限是 15 分钟。缩短计时的隔离验收触发了真实回收。原 API 退出后的完成记录和延迟发布恢复见 [会话启动恢复](session-startup-recovery.md)。

真实 SeaweedFS 验收复现了输出发布和另一个 API 的配额预留重叠。S3 发布沿用既有配额锁的有界等待，保留现有事务和发布回执。预留释放后，原关闭请求完成一次发布，目标文件字节与原生沙箱输出相符。

沿用原即时准入的对照在实际配额预留下返回 HTTP 409 `storage_quota_busy`。原生进程已经停止，关闭回执记为失败，尚无导出回执。[修复前并发回执](../../tmp/terminal-multisession-c0f8bc64/fixture-publication-observation.json)

| 验收 | 结果 |
| --- | --- |
| SQLite + 文件系统 | Chromium 驱动实际 cmd 与 WSL 沙箱。零订阅时产生一次输出，挂接原会话后收到回放。显式关闭发布文件，第二 API 可读历史。见 [浏览器回执](../../tmp/terminal-multisession-5cyzctzh/terminal-multisession-history.json)。 |
| PostgreSQL + SeaweedFS | 相同流程通过。第二 API 的真实配额预留与关闭发布重叠后释放，发布成功。两个 API 已退出，原生根进程与启动门独立核验退出。见 [浏览器回执](../../tmp/terminal-multisession-cvt0632p/terminal-multisession-history.json)、[并发发布回执](../../tmp/terminal-multisession-cvt0632p/fixture-publication-observation.json)及 [关闭回执](../../tmp/terminal-multisession-cvt0632p/close-receipt-wsl-sandbox.json)。 |
| 普通 cmd/WSL 和准备取消 | 见 [终端选区验收](FF-G07-terminal-selection-preview.md)、[终端准备与容量](terminal-preparation-capacity.md)和 [本机 PTY 生命周期](owned-pty-sessions.md)。 |

复验入口为 [terminal_multisession_history_acceptance.py](../server/terminal_multisession_history_acceptance.py)。本项使用真实浏览器、数据库、存储与原生终端，模型请求和模拟模型调用均为 0。
