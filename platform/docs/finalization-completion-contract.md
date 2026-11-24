# 收尾完成与对外通知的一致性

2026-11-04。N1-05原验收条件是：查询、SSE done、队列确认均不早于关键收尾；收尾失败不能成为成功。本次核对现有执行/证据/同步/发布/终态提交协议，补齐批次查询和成功事件广播两处遗漏。

批次列表、详情、提交重放及取消响应现在通过与单任务列表相同的查询读取成员最新attempt。数据库已经提交不可变产物、但兼容副本完成记录仍pending或invalid时，成员及整个批次继续显示running/finalizing，不提前累计成功数量。历史无attempt记录仍保持原查询兼容。

`jobs.updated`成功事件在outbox投递前检查其绑定attempt的完成可见性。pending/invalid时保留待重试，不广播成功；收尾可见性记录提交后仍以原事件ID投递。单任务查询、历史读取和SSE原有完成检查保持不变。不可变产物已经正式发布时，兼容别名修复失败不会撤销该发布，而是记录修复错误；这种可读取正式产物的情况与证据、同步、发布或数据库提交失败分开处理，实际别名修复继续N2-06/N5-04。

## 当前验证

- 真实Pandoc在SQLite/PostgreSQL各3个模式下运行：暂停兼容副本修复、完成可见性提交失败、可选别名修复失败。暂停时两个API的详情/日志/列表、历史批次视图和SSE一致保留未完成，成功事件不能被ack；释放后可见并通知。完成回执提交失败时正式不可变报告仍可读取，但Job对外保持未完成并显示收尾问题。批次视图由测试建立历史清单关联实际报告Job，不宣称现有批量接口已具备专业报告编排。
- 真实Redis/arq在两库各6项：三种模式分别观察暂停收尾前后。原生载荷实际退出0后暂停finalizer，查询仍running、SSE无done、Redis消息仍保留且worker未完成；释放后正常任务成功、finalizer异常任务失败。注入终态DB commit失败时arq `jobs_failed=1 / jobs_complete=0`，Job没有成功终态，SSE返回错误而非done。业务失败已持久化后，运输任务可以正常完成；运输成功不代表业务Job成功。
- `job_terminal_smoke.py --postgres`两库各9项：原API/另一API/SSE/持久原生清理证据一致，含旧终态、收尾失败、未核验清理、远端取消、断线和真实owner退出。
- 既有finalization专项8项：真实子进程、finalizer和DB提交门、错误证据、同步失败保留staging、提交失败、取消及迟到回调、可选通知异常。现有批量执行兼容1套通过。

原始证据：`tmp/pandoc-execution-evidence-4rxae73t/result.json`、`tmp/redis-finalization-evidence-32sdil0h/result.json`、`tmp/job-terminal-evidence-7ecedz8d/result.json`、`tmp/job-finalization-nfui6oif/result.json`。规范副本为 `tmp/mainline-N1-completion-contract-evidence/`。独立WSL Redis由OwnedProcess管理，实际停止已核验；未修改用户Redis服务、开发数据库或端口8000，没有业务数据库迁移。

## 仍需完成

finalizing跨重启后真正补交、回执修复和兼容文件恢复属于N5-04/N2-06，不能把本批的正确未完成显示当作已经恢复。完整取消竞态、未知进程清理和停机分别继续N4-04/N4-07；旧cancelled表示取消被接受，清理是否完成须查看实际停止证据。正式OpenAI登录联调按用户决定暂缓。所有53项主线及295项功能要求保留。
