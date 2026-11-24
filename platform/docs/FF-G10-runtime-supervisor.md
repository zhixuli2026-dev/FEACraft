# FF-G10 进程看门狗与跨 worker 作业监督

2026-11-19。两个独立 API 现在可从同一持久状态看到由 Redis worker 执行的作业，并通过同一个算例取消入口停止这些作业。作业收到取消请求后仍在活动列表显示 `cancelling`，直至原执行者完成实际进程树清理和持久收尾。单 API 形态下，一次算例取消同时停止了真实作业、终端、可视化和 DeepSeek 选择的长 shell。

## 原验收口径

G10 要求统一看门狗、任务所有者、租约、健康检查和进程树清理，使用 N4/N5 的故障恢复矩阵并覆盖多个 worker。FF-G10 对应 WP17、R0、T08，主线台账关联 N4-01、N4-07、N5-01。原先以容器编排替代看门狗的暂缓结论已经失效。T08 要求以实际执行核验状态、恢复、权限、资源与产物。[原审计](feature-gap-audit.md) · [交付计划第 2.2、5、7 节](full-function-delivery-plan.md) · [主线状态](mainline-task-status.json)

| 原要求 | 已有交付与证据 |
| --- | --- |
| 所有者、身份、租约 | 作业认领持久绑定 case、attempt、worker 出生身份、claim 和心跳。Windows 宿主及 WSL 原生身份先登记后放行。N4-01、N4-03 和 N5-01 已验收启动边界、有效所有者与失联判定。新共享活动查询只投影执行头所绑定的已认领作业，使用原所有权记录判断租约状态。[进程身份](process-identities.md) · [入口监督](native-entry-supervision.md) · [启动续租](job-launch-lease.md) · [监督查询](../server/app/services/supervisor.py) |
| 统一活动和健康检查 | 两个 API 通过 `GET /api/v1/runtime/processes` 读到同样的 Windows、WSL 活动作业。`/runtime/monitor` 返回两个活动作业及两份 `current` 租约，`/runtime/metrics` 输出同样的计数。已识别的过期和无法核验租约有相应计数与告警。普通活动列表保持算例所有权过滤。[路由与指标](../server/app/routers/runtime.py) · [本轮实测](../../tmp/runtime-supervisor-skxopr6q/runtime-supervisor-worker.json) |
| 取消和进程树清理 | 统一与逐作业入口共用持久取消。所有者通过 Redis 唤醒或既有心跳收到取消，在本机完成范围停止。同步响应包含仍在排空的作业。N4-02、N4-04 已覆盖放行竞争和停止证明。实际 WSL/MPI 专项记录两名非特权 rank、八个原进程身份均退出、输入保留及取消事件次序。[作业取消](../server/app/services/jobrunner.py) · [进程树停止](process-tree-stop.md) · [MPI 取消与事件](N7-01-mpi-cancel-events.md) |
| 失联、重启、发布 | N4-07 已验证两个实际 Linux worker 失联恢复路径，分别由新启动的 worker 和已运行的同伴扫描完成原发布。同一次 attempt 保留原所有者、停止回执、输入和既有发布数据，没有重新执行。N5 已核对活着但租约过期的本机所有者和未知身份的保守处理。终端正常关机和重启回读已有 cmd、非特权 WSL 与真实 S3 证据。[worker 猝停及恢复](worker-owner-recovery.md) · [真实失联回执](../../tmp/remote-worker-h1fpgpn7/owner-recovery.json) · [原所有者对账](job-owner-startup.md) · [终端正常关机](terminal-normal-shutdown.md) |

过期租约进入检查和对账。用户可据实际退出观察及持久停止回执判断进程树是否已清空。作业收尾、发布和取消后的 MPI 保留证据已有专项，统一资源预算的剩余入口继续挂在 N4-05。[资源预算台账](resource-budget-worklist.md)

## 两个 API 与两个实际 worker 的对照

两轮使用隔离 PostgreSQL、SeaweedFS 和 Redis 7.0.15。两个独立 Windows arq 进程在同一台主机上执行 Windows shell 和非特权 WSL 命令。独立 OS 探测观察到两个实际执行范围存活，WSL UID 为 65534。两轮均为基础设施验收，模型调用 0 次，模拟模型调用 0 次。

- 修复前，两份 API 都返回空活动列表，监控没有作业活动计数。算例取消返回 `ok: true`、空 `cancelled` 和空 `remaining`，两个作业随后仍处于运行中。测试使用逐作业取消清理，两棵树均经独立探测确认退出。[旧行为回执](../../tmp/runtime-supervisor-mf10h_9x/runtime-supervisor-worker.json) · [临时库回收](../../tmp/terminal-agent-postgres-3thihj5y/cleanup.json)
- 修复后，两份 API 返回相同的两条 `running/current`，监控和指标计数均为 2。无权用户的指定算例读取及取消返回 HTTP 404，其无筛选列表为空。第二份 API 的算例取消返回 `cancelled.job=2`，两条 `cancelling` 均保留在 `remaining`。随后两棵树分别取得 `scope_empty=true`、`verified=true` 的持久回执，独立 OS 探测确认退出，活动列表清空。[修复后回执](../../tmp/runtime-supervisor-skxopr6q/runtime-supervisor-worker.json)
- 正向验收的两个 API 已退出，两个 worker 均以退出码 0 退出。实际 Redis 停止已核验，测试 SeaweedFS 已退出，临时 PostgreSQL 数据库已删除。[基础设施清理](../../tmp/runtime-supervisor-skxopr6q/redis-stops.json) · [临时库回收](../../tmp/terminal-agent-postgres-0_u8vzsa/cleanup.json)

在项目根目录使用已配置的本地 PostgreSQL 和隔离验收入口。`--expect-missing` 仅用于原代码的基线对照。生产改动沿用已有表、持久取消和 worker 配置。回退涉及监督查询与路由，已持久化的取消仍由原执行协议收尾。[显式入口](../server/runtime_supervisor_worker_acceptance.py)

```powershell
& 'platform/server/.venv/Scripts/python.exe' -B -X utf8 `
  'platform/server/runtime_supervisor_worker_acceptance.py' --postgres --s3
```

## 同一算例的四类真实执行

在隔离 SQLite/FS 上同时启动 Windows 原生作业、cmd PTY、渲染真实 STL 的 VTK/trame，以及用户授权的 DeepSeek 回合。DeepSeek 选择并执行了一次 `run_shell_command`。真实 PowerShell 先写入随机启动标记，再进入等待。在发出取消请求前，正式活动列表和监控均识别出作业、终端、viz、Agent 回合及其 `agent-tool`，四个实际原生范围均经独立探测确认存活。[验收回执](../../tmp/live-runtime-supervisor-5m4m6je7/live-result.json)

一次 `POST /api/v1/runtime/processes:cancel` 接受上述五条活动项的取消。请求返回时，尚在排空的作业继续出现在 `remaining`。最终四个进程树都有可核验的持久停止凭证，独立 OS 观察均为退出，活动列表清空。Agent 原生工具落到 `cancelled`，SSE 恰好收到一次 `run.cancelled`，完成标记未写入。测试随后核对隔离 API 已退出。[停止回执及退出观察](../../tmp/live-runtime-supervisor-5m4m6je7/live-result.json) · [实际回合事件](../../tmp/live-runtime-supervisor-5m4m6je7/live-model-events.json)

使用已授权的 profile 26，`api.deepseek.com / deepseek-flash` 一次真实 HTTP 200。服务报告 5,077 tokens，模拟调用 0 次。测试入口在运行前校验审查涉及的 480 个文件散列。[显式入口](../server/live_llm_runtime_supervisor_acceptance.py) · [源码审查](../../tmp/FF-G10-runtime-supervisor-review-20261119.json) · [上游回执](../../tmp/live-runtime-supervisor-5m4m6je7/upstream.jsonl)

```powershell
& 'platform/server/.venv/Scripts/python.exe' -B -X utf8 `
  'platform/server/live_llm_runtime_supervisor_acceptance.py' --source-profile-id 26 `
  --review 'tmp/FF-G10-runtime-supervisor-review-20261119.json'
```

## 交互会话归属和整项边界

- PTY 的真实浏览器已在原实例挂接同一活动会话、回放断线期间输出，再显式关闭并核验根进程和启动门退出。第二 API 可读统一历史，关闭还在另一个实例运行的 PTY 返回 HTTP 409。它可读取原关闭完成的日志和回执。对活动会话的 WebSocket 若落在另一 API，则返回所属实例不可连接。[FF-G02](FF-G02-terminal-multisession.md) · [真实 PG/S3 浏览器回执](../../tmp/terminal-multisession-cvt0632p/terminal-multisession-history.json) · [终端处理路由](../server/app/routers/terminal.py)
- viz 已持久记录所有者、租约、根进程和就绪进程、停止证明。两个本机 API 实际启动两个不同端口的 VTK/trame kernel，已核验代理往返与关闭。另有真实 PG/S3 双 API 的渲染名额争用及释放证据。其他实例可以读同一持久记录。活动渲染的代理及关闭使用所属实例的会话对象，错路由的关闭返回 HTTP 409。[可视化会话](owned-viz-sessions.md) · [双 API 实际 kernel](../../tmp/mainline-N4-viz-evidence/concurrency-result.json) · [实际 S3 渲染名额](../../tmp/live-cli-startup-jf0i_3hl/viz-slots.json) · [代理](../server/app/routers/vizproxy.py)
- WP16 原文允许「PTY/viz/session 持久化或明确的 sticky+重建策略」，同时要求多副本避免重复执行。已交付的持久历史、原端控制和错路由拒绝覆盖其中的安全边界。当前 Nginx 对 API、终端 WebSocket 和 viz 资源使用同一普通 upstream，浏览器直连当前站点。活会话经负载均衡回到原所属端、所有者离线后的重建流程及跨主机验收由明确列出这些行为的 FF-J14 承接。FF-G14 继续承接按 session 的交互取消、清理与统一活动。[WP16](implementation-master-plan.md) · [FF-J14 与 FF-G14](full-function-delivery-plan.md) · [网关配置](../infra/nginx-web.conf) · [浏览器终端](../web/src/panels/TerminalPanel.tsx)

## 租约告警与整项验收

在 SQLite 与文件存储的独立双 API、双 Redis worker 实例中，对两条正在实际执行的作业记录注入过期时间和无法核验的所有者文档。两个 API 分别显示 `expired` 和 `unverified`，监控返回对应告警，指标各计 1。恢复原记录后，两条原生进程身份仍匹配、租约回到 `current`，随后由统一入口实际停止。此组观测的是公共 API 和指标。真实所有者失联及冷启动恢复沿用 N4/N5 回执。[租约故障回执](../../tmp/runtime-supervisor-3enx_hh9/runtime-supervisor-worker.json) · [N4-07 退出评审](N4-07-closure-review.md)

FF-G10 的原范围已通过组合验收。统一活动与取消覆盖同实例的四类实际执行，两个 worker 通过共享数据库交叉观察和停止作业。其他实例的 PTY 与 viz 由专用持久历史提供记录，多副本浏览器路由与逐会话操作继续由 FF-J14 和 FF-G14 验收。N4-05 保留统一资源预算范围。[运行时设置](../web/src/panels/SettingsPanel.tsx)

复跑租约故障验收时调用上面的 worker 验收入口并传入 `--lease-faults`，使用 SQLite 与本地文件存储。
