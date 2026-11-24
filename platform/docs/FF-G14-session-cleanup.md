# FF-G14 原生进程与按会话清理

2026-11-19。原审计 G14 验收单后端进程内的活动登记、所有者过滤、按算例取消，以及 `cancelled`、`remaining` 和 `errors` 的结果。WP12 同时要求按会话取消和清理，并将孤儿恢复接入持久状态。FF-G14 属于 WP12、R5、T08。[原审计](feature-gap-audit.md) · [WP12](implementation-master-plan.md) · [交付计划](full-function-delivery-plan.md)

## 统一历史里的按会话关闭

旧版浏览器可读同算例内的活动终端和渲染会话详情，详情没有单条关闭入口。现在用户可在统一会话历史中关闭当前服务持有的所选终端或可视化会话。按钮调用已有会话关闭 API，收到服务端完成结果后重新读取持久记录，把焦点移到已核验提示。已关闭的原记录仍可查看。[旧版对照](../../tmp/native-session-close-lxtn4gi6/native-session-close-browser.json) · [共享会话界面](../web/src/panels/NativeSessionHistory.tsx)

Edge 157.0.4341.60、独立 API、SQLite 和本地文件存储实测通过。按 Enter 关闭一个真实 cmd 终端，同算例的非特权 WSL 终端继续运行。打开实际 STL 渲染后，关闭选中的 VTK/trame 会话，其 iframe 消失，第二个 renderer 保持运行并留在列表。另一账户关闭四个目标均收到 HTTP 404。最终清理四个测试会话，持久回执均确认范围清空，独立 OS 探测确认根、启动门及 renderer 退出。隔离 API 已退出。模型调用和模拟调用均为 0。[最终回执](../../tmp/native-session-close-6a_s6ii6/native-session-close-browser.json) · [活动界面](../../tmp/native-session-close-6a_s6ii6/active-viz.png) · [关闭回执界面](../../tmp/native-session-close-6a_s6ii6/closed-viz.png) · [复验入口](../server/native_session_close_browser_acceptance.py)

## 所属实例、重启与按算例取消

| 原验收能力 | 既有真实证据 |
| --- | --- |
| PTY 身份与持久结束 | FF-G02 在真实浏览器挂接原终端、断开后回放、显式停止，并用第二 API 读取原记录。正常停机专项验证 cmd、非特权 WSL、实际 S3 暂存收尾及重启后文件和停止回执一致。[FF-G02](FF-G02-terminal-multisession.md) · [终端停机与重启](terminal-normal-shutdown.md) |
| 可视化身份与失联状态 | 两个本机 API 启动真实 renderer，各自校验就绪、代理和停止。所有者退出后可读取持久身份和核查状态，停止结论依据原回执。真实 S3 专项还验证双 API 渲染名额争用和关闭后的释放。[可视化生命周期](owned-viz-sessions.md) · [双 API 实际 renderer](../../tmp/mainline-N4-viz-evidence/concurrency-result.json) · [实际 S3 名额验收](../../tmp/live-cli-startup-jf0i_3hl/viz-slots.json) |
| CLI 所属范围与恢复 | 真实 CLI 的正常关闭、启动取消和原 owner 退出后恢复已有独立验收。恢复环境中的真实 DeepSeek CLI 回合读取随机文件内容，并留下该会话的实际关闭证明。[CLI 生命周期](cli-managed-session-lifecycle.md) · [CLI 失联恢复](cli-managed-session-recovery.md) · [真实 CLI 回合及关闭](deployment-recovery-runbook.md) |
| 统一活动与算例取消 | FF-G10 在同一算例同时运行实际 Job、PTY、VTK/trame 和 DeepSeek 选择的长 PowerShell。一次算例取消覆盖四个实际进程范围，独立确认退出。在等待清理时，作业继续出现在 `remaining`。Goal 已有独立活动登记入口。所链接的真实模型回执覆盖 Agent 回合。[FF-G10](FF-G10-runtime-supervisor.md) · [实际四类执行回执](../../tmp/live-runtime-supervisor-5m4m6je7/live-result.json) · [Goal 登记](../server/app/routers/agent.py#L1495) |

## FF-J14 的部署路由

活动 PTY 和 viz 由创建它的 API 实例控制，其他实例可通过专用历史入口读取持久状态。当前网关使用普通 API upstream。将多副本的终端 WebSocket、渲染代理和关闭请求路由至正确实例，以及滚动恢复、负载均衡和跨主机联调，按 FF-J14 的原范围继续验收。[网关配置](../infra/nginx-web.conf) · [FF-J14 原任务](full-function-delivery-plan.md)
