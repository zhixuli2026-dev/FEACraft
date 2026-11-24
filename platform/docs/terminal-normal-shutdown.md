# 终端正常关机收尾

2026-11-17。API 正常关机现在等待后台终端停止、完成文件同步和持久记录；浏览器断开后终端继续运行的重连语义保持。

## 问题与修复

原 API shutdown 没有关闭终端会话。实际 cmd 写文件并断开后，请求 Uvicorn 正常退出，API 和终端根进程退出，但数据库仍为 active、stop_json 为空。复现保留在 `tmp/terminal-graceful-shutdown-5du13bwh/shutdown-result.json`，明确未通过。

现在 shutdown 调用终端集中关闭入口，停止并等待 reaper，合并正在进行的关闭请求，复用原 `TerminalSession.close` 完成范围停止、输出排空、暂存同步及持久收尾。正常关机使用 `shutdown` 原因，已有用户取消保持其原关闭任务。某类会话关闭失败仍尝试其余会话类别，最后报告错误。

实际验收又发现普通 WSL 终端沿用发行版默认 root。按用户已选定的非特权执行要求，`OwnedTerminal` 为普通 wsl 传入现有执行器支持的 `--user nobody`；cmd 和 wsl-sandbox 启动分支不变。未增加新的权限控制层。

## 实际结果

入口 `platform/server/terminal_shutdown_sessions_acceptance.py`；审查 `tmp/N4-07-terminal-shutdown-sessions-review-v2-20261117.json`，642 个生产和辅助源码散列。

| 环境 | 证据 | 结果 |
| --- | --- | --- |
| SQLite + 本地文件 | `tmp/terminal-shutdown-sessions-fs-qx8riguo/shutdown-result.json` | cmd、WSL 两会话通过 |
| SQLite + 实际 SeaweedFS S3 | `tmp/terminal-shutdown-sessions-s3-5y079w46/shutdown-result.json` | cmd、WSL 两会话通过，S3 服务已停止 |

每组验证真实 shell 写入随机内容、WebSocket 断开后会话仍存活、实际 Uvicorn 正常关闭、原进程出生身份退出、数据库 closed/verified/ended_at/error、停止原因 shutdown，以及重启后历史会话和原文件 SHA-256 一致。普通 WSL 实测 UID 65534；S3 暂存目录已删除。四次 API 正常退出均为退出码 0。

前两轮因实际 WSL UID 0 在关机步骤前失败，未算通过；四个会话和 API 的记录进程均独立核验退出，S3 已停止，原源码与失败保留于 `tmp/terminal-shutdown-uid-failure-20261117`。最初补丁审查因 CRLF/LF 字节差异拒绝，逐文件确认文本与预览完全相同后登记实际散列；没有绕过源码审查。

本轮模型调用 0、模拟模型调用 0、Claude 调用 0，属于实际基础设施验收。此前 child 恢复的六次真实 DeepSeek 调用单独见 [子执行交接恢复](child-handoff-loss-recovery.md)。本轮不泛化为物理多主机、所有数据库/存储组合或系统重启验收；这些原范围仍由 N7-01/N4-05 承接。
