# N4-03 当前入口核对

2026-11-06，已按原条件完成。原验收条件：MPI ranks 和后台 kernel 无残留，会话断开与用户取消的行为有区分。完整资源预算、重试/续算、跨主机恢复继续分别由 N4-05、N5-05、N4-07/N5 承接；这些仍在总目标内。

## 当前机器盘点

重新生成并校验 `execution-entry-inventory.json`：316 个 Python 源文件、241 个路由、355 个边界调用；其中 21 个原生进程调用点。本轮最初盘点为356个；注册处理器改为持久投递后，移除一处直接 `start_job` 调用。旧清单覆盖 297 个文件，最初盘点有 60 个源码摘要变化、7 条新增边界记录。原始只读差异留在 `tmp/N4-current-entry-diff.json`，旧清单保留在上一份源码归档中。

新增记录逐项核对：

- `main.shutdown` 的 `maintenance_task.cancel` 是存储维护 asyncio 任务，随后 await gather，不是 OS 进程退出证明。
- `case_events.append_case_event`、`provenance.record_case_revision` 与 `casedict.update_case_dict` 是构造/兼容文件写入辅助；活动文件协调与崩溃恢复已由 N2 独立核对，不能拿这些同步辅助代替活动写事务。
- `file_transactions._apply` 的 `os.replace` 是带原内容与日志校验的文件事务替换。
- 两条 `ToolExecutor.apply_proposal` 的 `current.replace` 实为字符串替换，由宽匹配扫描器捕获；后续真实写入进入 `write_case_dict` 或文件事务。这两条不是进程启动或直接文件替换。

这 7 条没有新增原生启动调用点。六个已有监督相关模块的源码发生变化：Agent/terminal/viz 路由、agent_shell、codex_sessions 和 jobrunner；仍需结合真实运行证据核对其当前接线，不能据“没有新 Popen”直接关闭 N4-03。

## 现有证据与剩余核对

MPI 实际 ranks 与取消/限额/owner 猝死、PTY 的断开重连与显式关闭、viz kernel 的退出，以及报告/LaTeX/专业编译子执行已有分批证据。入口定位文档包括 `native-entry-supervision.md`、`owned-pty-sessions.md`、`owned-viz-sessions.md`、`owned-native-children.md`、`professional-process-faults.md`。这些旧记录用于定位实现，不直接冒充本轮验收。

下一步对照当前 21 个原生调用点及所属启动门/监督范围，选择能证明原条件的真实 MPI、PTY、viz 和编译检查，并核验停止回执与实际出生身份退出；保留外部服务、任意跨运行时脚本和远端主机的明确边界。N3 报告当前验收仍在运行，整项任务数量不因本次机器清单刷新改变。

## 原生调用点所属范围核对

| 调用点分组 | 个数 | 当前所属与退出机制 |
| --- | --- | --- |
| `owned_process` / `process_gate` | 5 | 根进程准备、出生身份登记、放行门、Windows Job 与 WSL 私有范围；具体实际退出仍由对应运行时测试证明 |
| `terminal_bridge` / `terminal_pty_gate` / `stdio_bridge` | 4 | PTY/CLI 双层门；根与实际 shell/CLI 身份登记后才放行，会话关闭等待所属范围；断线重连是单独的会话语义 |
| `agent_shell_native` | 1 | 已批准 Windows shell 在登记门的所属范围内创建，shell 文本保留原义；不能把任意外部服务当成本机后代 |
| `control_identity_reader` | 1 | 固定只读身份/ancestry 调用，原生 SIGALRM 与 owner 管道 EOF 期限；宿主保留创建句柄并在 finally 等待退出 |
| `runtime_native` / `synctex_native` | 2 | 固定用途的原生准备/只读请求，不伪装为工程 Job；停止证据由请求所属记录保留 |
| `latex_native` / `pandoc_native` | 3 | worker 与发布复验使用所属子执行协议，工具探测同样在所属范围内；停止未核验时不可发布 |
| `mesh_worker` / `mpi_native` / `udf_native` / `flotherm_library_native` | 5 | 网格在封存 WSL worker 内；MPI/UDF/库 worker 通过 `require_supervision` 和 `run_native` 调用固定原生协议；MPI 字典查询也在该原生范围中 |

总计21处。MPI、UDF 与库 worker 的客户端无监督回退路径未被重新放开。上述为代码接线核对，不能替代实际 ranks/kernel/编译器退出观察。

本轮 MPI 双库各6项、终端双库各44项已全部通过，进程退出0，临时数据库已清理。MPI 覆盖取消、超时、日志上限、错误UID/发行版和API猝死，核验实际 ranks 退出与外部哨兵存活；终端覆盖 Windows/WSL 重连、输入、登记/停止故障、后代退出、owner猝死与新API只读历史。证据分别为 `tmp/mpi-scope-evidence-us03h4d6/result.json` 和 `tmp/terminal-evidence-k5adgx9h/result.json`。后续可视化与编译终态现已补齐，见下文。

本轮可视化双库各33项通过，进程退出0，临时数据库已清理：`tmp/viz-owned-evidence-blkxm883/result.json`。包括真实VTK首帧/代理、权限、视图关闭与工程Job取消区分、重复关闭、启动/登记/停止故障、脱离后代和owner猝死。原生编译与UDF专项的最终结果已补齐，见下文。

## 最终核验及保留范围

原生编译当前SQLite/PostgreSQL各15项通过，实际LaTeX worker与finalizer子执行、取消/超时/日志上限、登记/停止/回执故障、owner猝死及新API持久历史均核验；UDF的真实wmake/编译器另有4项通过。结果、退出码及源码绑定见 `tmp/mainline-N4-supervision-closure-evidence/validation.json`。本轮校验的是当前316个源文件中的21个原生调用点及其公共监督路径；不是任意外部服务或跨主机进程的退出保证。

按原条件关闭N4-03。此前remaining中混入的要求完整保留：取消/发布竞争由N4-04；整次预算/配额由N4-05/N4-06；停机、跨主机和未知停止恢复由N4-07/N5；专业MPI/UDF重试续算由N3-07/N5-05；模型输入、账户/额度、原生会话和容器部署由FF-F15/FF-F54/FF-F55/FF-F58/FF-G14/FF-J16及相关全功能项继续承接。正式SIWC联调仍按用户决定暂缓，完整环境组合由N7验证。没有因此关闭任何295项严格交付条目。

后续N4-05增加execution_budget后，当前清单为317源文件/241路由/356边界，原生调用仍21处；新扫描记录at.replace是datetime时区补齐，非文件替换或原生启动。N4-03收口时的316/355清单与原始证据保留历史语境；当前预算的真实原生退出另由native_budget_smoke双库各2项覆盖。
