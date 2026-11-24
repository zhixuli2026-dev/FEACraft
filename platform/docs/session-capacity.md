# 会话准备额度、真实模型配置验证与沙箱关闭缺陷

2026-11-07：沙箱终端强制关闭丢输出已接入独立受控导出、文件事务和持久发布证明，并修复并发沙箱共用PPID根目录。FS/S3实际新增、修改、删除及冲突验证通过；78条基础设施、3条数据库协议检查，0模型调用、0模拟LLM。持有者猝死/恢复、空目录、大批量及完整资源矩阵仍待。主线33/53、剩20；资源11/17、剩6。见[沙箱导出与剩余工作](sandbox-export-lifecycle.md)。

2026-11-06。完成 CLI 会话设置、PTY 与可视化准备入口的额度增量；发现并确认了仍未修复的 WSL 沙箱关闭丢文件问题。本阶段不能作为全部终端或沙箱功能通过的证明。主线完成 33/53、剩 20 项；资源清单完成 11/17、剩 6 块；295 项完整交付范围不变。

## 产品修改

CLI 会话设置、终端 `_terminal_access`、可视化 `_create`/`_reauthorize` 使用已验收的 `committing`：在算例协调器隐含恢复之前获得所有者额度，持有至 SQL 提交和文件日志收尾。终端启动、可视化启动在额度释放后执行。无绑定算例的终端仍仅提交自己的授权读取事务，并等待提交结束。

终端 WebSocket 明确返回额度不足状态。启动拒绝返回 `storage_capacity` 和实际状态码，关闭码 1013。已有终端输入或窗口调整遭遇 429、503、账户额度忙 409 时，只拒绝当前消息，保留连接与 PTY；恢复后需要客户端重新发送。身份和权限失效仍走原来的关闭路径。实际验证了 429 恢复；409/503 分支尚未单独故障注入验证。

## 通过的实际证据

入口 `platform/server/session_capacity_checks.py`。实际运行产品 API、数据库、版本化 SeaweedFS S3、cmd、WSL、VTK/trame；模型组使用既有授权 DeepSeek profile 26。观察包装始终调用真实函数，不提供模拟模型、CLI、终端输出或渲染结果。

| 组 | FS / SQLite | FS / PostgreSQL | S3 / SQLite | S3 / PostgreSQL | 每组检查数 |
| --- | --- | --- | --- | --- | --- |
| cmd | `xkuh3fla` | `y703jdoy` | `qwja5xf7` | `ytmm9laq` | 5 |
| wsl | `i1_5t4f8` | `zc8yseix` | `kolwqgyi` | `64o3g3kr` | 5 |
| viz | `time6wsk` | `z6dfdq7w` | `zylkjphr` | `_7_86qxu` | 5 |
| settings | `kxx_a01k` | `fm5m0f7f` | `5qqsll7a` | `2q53vfda` | 4 |

表中证据完整路径为 `tmp/session-capacity-<编号>/checks.json`。16 个通过组共 76 条检查，其中 4 条为真实模型回复检查、其余 72 条为实际基础设施/配置检查。隔离 API 均退出，8 个临时 PostgreSQL 库的删除回执保留。最终通过组 API 日志无未处理异常栈。开发库和端口 8000 未操作。

终端检查包括零额度不创建进程/不增加物理用量；实际启动时额度已释放；零额度下输入/调整尺寸拒绝但连接和进程仍可用；恢复后原连接通过实际 shell 读取文件中的随机值；关闭后保存退出证明和输出文件，被拒绝的写入没有执行。

可视化检查包括零额度不启动、缺失来源释放额度、真实 VTK kernel 启动、HTTP 客户端页面可读取、实际关闭与日志保存。这里只证明页面被实际服务，不代替浏览器图像/交互验收。

会话设置检查包括零额度拒绝且旧值/物理用量不变、无权限或不存在的 profile 拒绝、实际设置提交，以及使用返回的 profile/model 经平台消息接口请求真实模型。这不是原生 Codex CLI 推理的验收。

## 真实 LLM

4 次 `https://api.deepseek.com/v1/chat/completions`，全部 HTTP 200，返回模型 `deepseek-flash`。各组保存了 `upstream.jsonl` 的响应身份和 usage，以及 `live-events.json` 的真实流式事件。本批总 tokens 为 13,008（3,287 + 3,214 + 3,295 + 3,212），没有模拟调用。

累计真实推理请求由 62 增至 66，已报告 tokens 下限由 252,520 增至 265,528。此前 4 次取消/断线缺少最终 usage，仍不能按零消耗计算，也不把全部历史请求称为全部验收通过。先前记录见 [Agent 模型与取消证据](agent-memory-stream-lifecycle.md)。密钥只从已有加密配置读取并进入隔离加密 vault，未输出到日志或文档。

## 失败与优先修复项

最初两个组 `9zzglkmm`、`boy9r37f` 在创建测试算例时失败：测试观察器的全局变量覆盖了配额夹具保存的原函数，未进入目标会话路径。已改用独立变量名，失败日志保留。

`tmp/session-capacity-_p4yp5ok/checks.json` 是真实产品失败：WSL 沙箱成功启动、实际写入并读回随机值，额度拒绝及原连接恢复均正常；调用 DELETE 关闭后，API 返回成功、进程清理证明为真，但项目中输出文件为 404。该组没有计入通过的 76 条。

使用上一源码归档的原终端路由重新运行同样的真实操作，结果相同：`tmp/sandbox-close-baseline-63cm3m_1/defect.json`。复现只在隔离进程加载旧路由，不修改当前工作树、不替代模型输出。这证明缺陷并非本次额度封装引入。

根因：`sandbox_wsl.sh` 将工作副本放在 128 MiB tmpfs 中，只在内部 shell 正常退出后复制到 `/output`。`OwnedTerminal._stop` 结束完整进程范围时，可能连复制步骤一起停止；`TerminalSession._close` 只凭进程退出和外层 staging 收尾就记录 closed，没有证明沙箱内副本已导出。不得改测试为只验证输出回显，不得把提前发送 exit 当成强制关闭已修复。

下一优先工作：建立在结束进程范围前后仍有效的沙箱输出保留/导出协议，区分“进程已退出”与“沙箱输出已保存”，并复验主动关闭、正常退出、超时/取消、持有者死亡、导出失败与来源并发修改。设计必须保留 namespace 隔离、非 root 执行和现有工作副本限制；不能简单改成直接写共享项目目录，也不能仅延长等待时间后宣称不会丢失。此项归入 FF-F58/FF-J16 和后续原生资源生命周期的未完成范围。

其他仍未完成：`codex_sessions.Session.access`、启动请求、原生 fork/turn 收尾的恢复额度；技能/远程导出；FS 少量正余额的恢复元数据峰值；上传/派生跨 worker 与死亡矩阵；RB-13 至 RB-17 的完整生命周期、长 I/O/队列、维护、全局并发及目录外强制限制。

源快照：`platform/backups/mainline-session-capacity-20261106.zip`；检查索引：`tmp/mainline-session-capacity-evidence/validation.json`；后续任务：`tmp/session-capacity-pending.json`。该快照明确携带上述未修复缺陷，不代表全功能验收完成。
