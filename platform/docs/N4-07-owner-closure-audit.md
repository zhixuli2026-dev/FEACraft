# N4-07 关闭与恢复的具体执行归属

2026-11-17。按原条件“关闭与用户取消可区分；worker 只在完整收尾后确认任务；失联可恢复”核对。最新退出评审已完成，主线 49/53，详见 [退出评审](N4-07-closure-review.md)。以下保留当时核查与复现；终端最终修复和实际验收见文末。

## 当前核对

| 执行归属 | 当前依据 | 结论与后续 |
| --- | --- | --- |
| Redis JobAttempt 的根执行与专业子执行 | `job_recovery`、`job_scope_recovery`；原真实 worker 关闭、运行中失联、根准备与 child 部分观察恢复证据 | 根执行和已登记守护的 child 已验收；原分配/守护交接增量正在实际验收，不提前判定完成 |
| API 本地执行和后台收尾 | `main.shutdown` 等待 workflow/fallback/delivery/publication finalization 停止；各请求沿原所属作用域收尾 | 保留原预算与故障矩阵要求；不因根进程退出推定未知写者已停止 |
| Viz 会话 | `main.shutdown` 调用 `close_all_viz_sessions`；既有会话执行、关闭和历史证据 | 集中关闭入口存在；历史 S3 可控适配层证据不等同实际远端服务验收 |
| Codex 原生会话及启动请求 | `main.shutdown` 分别等待 `codex_session_requests.close_all` 与 `codex_sessions.close_all` | 已有真实模型会话关闭/恢复证据；更广存储与资源组合仍按原 N4-05/N7 范围核对；Claude 已由用户取消后续接入 |
| 终端 PTY 会话 | `terminal_ws` 的 finally 只取消转发任务、取消订阅，明确保留 PTY 供重连；`main.shutdown` 未调用终端集中关闭，终端模块目前也未定义该入口 | **已实际复现，待修复**：真实 cmd 写文件、断开连接后会话保持 active；请求实际 Uvicorn 正常退出，API 退出码 0，原 API/终端根出生身份退出，SQLite 仍为 active、stop_json 为空。应复用现有 `_stop_terminal`/会话 close 完成持久收尾 |

终端断开后继续运行是既有产品语义，应保持。待修复的是 API 自身关闭这一生命周期边界。需要实际核验断开后会话仍存活、API 正常关闭后停止记录完成、实际进程退出及重启读取一致；涉及模型回复的部分仍使用真实服务。

## 保留的原范围

N2-06 承接的活动/未知写者仍阻止接管；不能用过期租约、空 PID 或本地观察推断另一主机已经停止。已有 Windows API/Linux worker 使用不同实际 host ID 的证据，不冒充物理多主机验收。完整跨存储/多副本故障矩阵、部署环境与手册仍由 N7-01/N7-05 对应任务保留。

复现证据：`tmp/terminal-graceful-shutdown-5du13bwh/shutdown-result.json`，明确 `passed=false`；模型调用 0、模拟模型 0。`terminal_shutdown_acceptance.py` 使用隔离 SQLite/FS 和实际 Uvicorn `should_exit` 路径，不替换产品关闭函数。首轮 `tmp/terminal-graceful-shutdown-ejstw6e4` 因测试误读 API 未公开的存储前缀，在创建 PTY 前失败；已改为只读隔离 SQLite，失败记录保留。审查文件为 `tmp/N4-07-terminal-shutdown-review-v2-20261117.json`。

本页把“其他外部 owner”展开为可核查对象，尚未给出整个 N4-07 的完成结论。终端关闭缺口应在当前 child 交接实际验收后优先处理，不能用交接场景通过替代修复。

## 最终补齐

child 分配/守护交接九组实际场景已通过，首轮容量失败及续验分别保留。终端集中关闭入口已接入，普通 WSL 已使用现有 nobody 执行配置；实际 FS/S3 + cmd/WSL 四会话正常关机、独立退出和重启文件读回均通过。详见 [终端关机收尾](terminal-normal-shutdown.md)。原始 N4-07 条件完成；跨环境完整矩阵和统一资源预算继续留在 N7-01/N4-05。
