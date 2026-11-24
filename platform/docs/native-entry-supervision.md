# N4-03：原生执行入口监督

N4-03.h：核对236个app源码的旧worker/条件回退；退役无正式调用者的report_worker，Pandoc/LaTeX必须具有所属通道，CLI发现不再使用默认WSL runner。通道14、发现7、LaTeX双库各15、Pandoc双库各19、原生准备14、文档准备16、普通文档23、快照10通过；首轮误删导入失败及修正重跑分别保留。53/295、11完成/16实施中/26待实施及head3d1c98e2b754/35表不变。 详见[原生编译所属通道](required-native-channels.md)。

N4-03.h：复现并修复API退出后WSL只读身份探测残留；身份/ancestry改用所属Windows范围、固定Linux读程序、10秒原生期限及管道退出。专项11（取消0.328秒/API猝死0.671秒/期限10.532秒）、登记双库各20、PTY双库各44、CLI/基础兼容5/5通过（实际Linux CLI10、准备9、传输7）。不创建业务Job，不改变原持久登记后放行；53/295、11完成/16实施中/26待实施和head3d1c98e2b754/35表不变。 详见[所属只读身份探测](owned-control-readers.md)。

N4-03.h继续：真实CalculiX增量计算三种中断3项、FloTHERM实际编译4项、PPT/PDF/FloTHERM转换实际处理检查点中断9项、共享夹具UDF兼容4项通过。worker检查点为明确测试暂停；部分转换输出可保留，不代表原子发布或N5恢复。仅测试/文档改动；53/295、11完成/16实施中/26待实施、head3d1c98e2b754/35表不变。 详见[专业进程故障后续批次](professional-process-faults.md)。

N4-03.h第二批：专业启动冲突现在保留已提交Job ID；九入口登记拒绝及无Job/跨用户边界共11项、真实UDF编译器4项、ParaView/CadQuery实际库与脚本后代6项、兼容6/6及SQLite/PostgreSQL登记各20项通过。owner猝死仅证明实际退出；跨运行时后代、其余专业停止和N5恢复继续保留。head仍3d1c98e2b754/35业务表，开发库未动；53/295及11完成/16实施中/26待实施不变。 详见[专业进程故障](professional-process-faults.md)。

N4-03.h后续：WSL私有目录初始化已从直接subprocess移入登记后的Linux bridge；准备后重新核验配置、访问权和执行归属，再单独放行CLI。初始化专项9、双库会话各16、实际Linux CLI10、目录13/传输7/文件sandbox、宿主兼容5/5及profile历史/拒绝7项通过。owner变更仅清理自己持有的进程，不伪造持久回执；残留目录/未知结果继续N5。head仍3d1c98e2b754/35业务表，开发库未动，53/295和11完成/16实施中/26待实施不变。 详见[所属CLI初始化](owned-cli-state-preparation.md)。

当前进展（2026-11-03）：g2b3i 的新启动入口已补齐 WSL 实际 10 项验证；h 首批修复 CalculiX/SU2 的 shell 路径展开，实际求解 4 项、SU2 停止故障 3 项、专业兼容 8/8 通过，复核 13 个 Job/3 个原生子执行的停止回执。具体入口结论和未完成的准备 helper、库内派生及专业故障矩阵见[专业入口监督](professional-entry-supervision.md)。下文较早增量记录保留为历史。

N4-03.g2b2：宿主Codex原生回合/SSE、幂等消息、复杂审批/计划问答、显式线程续接及API/Goal/审批共用执行归属已接通。双库各34回合、8暂存、6双API及115迁移约束通过；旧CLI会话各34、Agent shell各32、core14/14、协议辅助7/7通过，真实CLI本地Responses fixture6项通过，无远端推理或个人登录。head f97e54a8d310、30业务表；开发库旧13表未迁移/重载。下一单元g2b3账户登录/登出、sandbox状态/设置、WSL原生CLI与完整UI，随后专业入口、N4/N5；53/295和11完成/16实施中/26待实施不变。详见 [owned-cli-turns.md](owned-cli-turns.md)。

N4-03.g2b1：宿主CLI持久会话和正式API已接通；两级实际登记后放行，私有账户/临时目录、账户/模型展示、唯一线程创建、case/S3收尾与历史保护通过双库各34项、S3可控各9、迁移各87约束，原Agent shell各32及core14/14通过。真实Codex经正式路由读取空账户/8模型、创建线程并核验关闭，无模型推理。head e86d43f7c209、26业务表；开发库旧13表未迁移/重载。下一单元g2b2持久回合/SSE/复杂审批，再登录/WSL/UI及专业入口、N4/N5；53/295与11完成/16实施中/26待实施不变。详见 [owned-cli-sessions.md](owned-cli-sessions.md)。

N4-03.g2a：所属stdio和实际Codex协议基础已验收；22项进程/通信专项、7套定向回归，以及真实CLI对本地Responses fixture的完成/失败/中断/恢复/分叉通过，空账户且无远端推理。双启动回调尚未接数据库；下一单元g2b接用户/会话持久台账、正式API、账户/复杂审批、case/S3及WSL原生CLI，再继续专业入口和N4/N5。head仍d75c32e6b1f8/25业务表，开发库未迁移/重载，53/295与11完成/16实施中/26待实施不变。详见 [owned-codex-transport.md](owned-codex-transport.md)。

API Agent shell增量：ToolCall唯一NativeRequest，原子审批、实际根/来源/权限复验及停止核验通过双库各32项，S3可控各9项。head d75c32e6b1f8、25业务表；g2实际Codex/CLI及完整N4-03继续，详见 [owned-agent-shell.md](owned-agent-shell.md)。

viz增量已验收：NativeVizSession、放行前根登记、实际kernel首帧、整树退出/缺回执、持久历史和copy/trash保护通过双库各33项；S3可控各9项，本机双API端口竞争通过。head c64b21d5a0e7、25业务表，完整N4-03继续Agent/CLI及专业入口。详见 [owned-viz-sessions.md](owned-viz-sessions.md)。

PTY后续增量：NativeSession按用户/可选case保存根与实际gate出生身份、租约及停止回执；真实重连、退出/缺回执、2MiB输入与脱离后代清理通过双库各44项，S3可控各9项。代码head b53a10c4f9d6、24业务表；详见 [owned-pty-sessions.md](owned-pty-sessions.md)。

全局Python/WSL能力探测已接入按用户所属、case为空的请求范围，固定用途、实际退出与降级语义通过双库各25项；不分配Job。代码head ad42f9b3e8c5，仍23业务表，详见 [owned-runtime-probes.md](owned-runtime-probes.md)。

后续SyncTeX只读请求也已接入锁外NativeRequest，历史映射/旧版来源重验、HTTP断线与实际退出、viewer权限复核通过双库各25项；继续其余探测和会话入口，详见 [owned-synctex-requests.md](owned-synctex-requests.md)。

后续正式整合：报告/LaTeX提交及工程报告原生准备已移出case写事务；HTTP断线、幂等和来源重验通过本批隔离验证，详见 [document-admission.md](document-admission.md)。原有正式作业、独立内容验证与发布协议继续适用。

后续增量：独立准备请求归属基础及双库各14项验证已落地，正式业务锁外准备、来源重验和HTTP断开仍在下一单元接通；见 [native-request-scopes.md](native-request-scopes.md)。本页正式作业协议继续适用。

N4-03.c中正式报告Pandoc解析/编译/版本及API复编译已接入API所属host范围，双库各16项；另修复短命成员退出与出生读取竞态，12项生命周期和双库树各15项通过。准备探测/解析仍需锁外request owner，SyncTeX及会话入口继续推进，详见 [owned-pandoc-children.md](owned-pandoc-children.md)。

N4-03.b及c中的LaTeX正式/独立复编译现已接入API所属子执行：worker工具探测也使用同一协议；双库各15项及新API持久读取通过。登记与清理失败均阻止放行或发布，详见 [owned-native-children.md](owned-native-children.md)。下表保留入口拆分；准备探测/Pandoc/SyncTeX及会话仍为后续工作。

历史记录（前置接线）：独立复编译移出写事务，提交重验所属快照和发布条件；同时修复finalizing及另一API取消，双库各10项通过。详见 [publication-preflight.md](publication-preflight.md)。内部子执行的持久登记、父级控制和退出等待仍是下一实现单元。

日期：2026-11-02。N4-03 实施中；首批完成 MPI 非 root 启动与真实 ranks 清理。主线完整范围为 53 项，全功能完整范围为 295 项。入口盘点以 [执行矩阵](execution-entry-matrix.md) 和 [机器清单](execution-entry-inventory.json) 为准，整项状态以 [主线台账](mainline-task-status.json) 为准。

## 已交付：MPI 原生身份与停止

在隔离实例复现旧 MPI 命令失败：`runuser: cannot set groups: Operation not permitted`。N4-02 引入私有 user/mount/PID namespace 后，原命令在仅映射当前用户的 namespace 内再次切换 nobody，无法设置另一组身份。此前的 MPI 成功证据不能代表该组合继续可用。

现在由固定 argv 在进入监督启动门之前指定 `wsl.exe -d <配置发行版> --user nobody --exec ...`，再用 `unshare --map-current-user` 保留原生 UID 构建私有 namespace。真实 ranks 使用非零 UID；没有添加 MPI 的 root 运行豁免。WSL 的账户选择参数见 [微软 WSL 命令文档](https://learn.microsoft.com/en-us/windows/wsl/basic-commands)。实际身份与 namespace 映射由本机运行证据核验。

`owned_process` 仅扩展这一明确的 nobody argv 形状，不提供任意账户切换入口。原生启动门报告身份后，父进程独立复核出生身份；非 root profile 若报告 UID 0，在用户载荷放行前失败。登记 running 还会核对 attempt 中的发行版与提交 spec 的显式发行版，拒绝 root 身份替换或发行版替换。MPI 入口将已记录的启动/登记失败转换为 HTTP 409，身份冲突提供 `code/message/job_id`，避免返回未分类的 500。

分解、求解、重构继续运行在本次拥有的私有 namespace 中，沿用 N4-02 的 Windows Job 与 WSL 原生退出核验。取消、墙钟超时、日志限额和 API owner 猝死后，测试分别探测实际捕获的两个 icoFoam rank 及其辅助进程的出生身份。停止记录仍使用现有 `attempt.process_identity/process_stop`；没有新增表、迁移或公共预算字段。

MPI 准备仍使用 case 写协调，已有临时目录分解与完成后回传逻辑保持当前契约。本批没有完成输入封存、不可变 MPI 版本发布、旧 processor/force 管理、临时目录回收、集群调度或重启恢复；这些继续归入 N3-07、N4-05、N5 和 FF-C06B。API 猝死后的实际进程清理不等于已经持久保存终态，本批不会伪造猝死后的完成记录。

## 证据与复验命令

汇总：`tmp/mainline-N4-mpi-evidence/`。

| 验证 | 本轮结果与边界 |
|---|---|
| `mpi_smoke` | 隔离 writes 单套通过；串行与两子域重构速度场相对差异 `1.14e-08`，阈值 `1e-5`；新增非零原生 UID 和 verified/native exited 断言 |
| `mpi_scope_smoke --postgres` | SQLite、临时 PostgreSQL 各 6 项通过：取消、超时、日志上限、错误 UID、错误发行版、实际 API owner 猝死 |
| 真实 ranks | 每个运行场景捕获两个 icoFoam；相同私有 PID namespace，原生 UID 非零且 uid_map 为该 UID 到自身的单一映射；停止后捕获成员全部 exited |
| 失败与副作用 | 错误登记没有已提交的运行身份，等待门退出；取消/失败不回传 processor 结果，原输入哈希不变，外部 Windows/WSL 哨兵继续存活 |
| 登记兼容 | SQLite/临时 PostgreSQL 各 20 项通过，包含未登记不能放行、错误身份、取消竞态和跨 API 读取 |
| 通用兼容 | core 14/14；契约 7/7（claim、finalization、启动门、进程树、树竞态、身份、生命周期） |

超时 8 秒和 1 MiB 日志上限仅由测试独立 app 注入已有 JobProcess 参数，未把它们描述为新 MPI 公共 API。API 猝死由测试 app 自身退出，确认的是实际 owner 的出生身份，不是误杀 venv 转发器。所有库、目录、API 端口与测试进程独立；临时 PostgreSQL 库在 finally 回收。

```powershell
platform/server/.venv/Scripts/python.exe -X utf8 -B platform/server/mpi_scope_smoke.py --postgres
platform/server/.venv/Scripts/python.exe -X utf8 -B platform/tools/run_regression.py --profile writes --only mpi_smoke --logs tmp/mpi-recheck
```

失败原始证据保留：`before/mpi_smoke.log` 为 runuser 故障；`tmp/mpi-scope-lyzxrg77/api.log` 为首次负向登记返回 500。最后通过结果来源及运行顺序见 `evidence-origin.json`，不以旧结果替代本轮验证。开发实例未迁移、未重载；未重跑全部 offline/writes/CAE/前端套件。

## 全部入口接线任务

| 子任务 | 当前代码与缺口 | 实现与验收要求 |
|---|---|---|
| N4-03.a MPI | `routers/mpi.py → start_job` 已接通本批非 root 原生范围；串并行和中断证据已通过 | 保留真实 ranks 验证；随后与 N3-07 注册处理器/发布及 N5 恢复组合复验 |
| N4-03.b 内部 WSL 编译 | `registered_latex_worker → native_execution_client → API NativeExecutor → OwnedProcess → latex_native` 已接入API持有的WSL范围，双库专项通过 | 子执行绑定父 attempt/token，放行前登记实际 native 身份；父取消、根退出与 owner 猝死必须覆盖这些后代；父终态等待子执行核验 |
| N4-03.c API 复编译与探测 | LaTeX和正式报告worker/API复编译已接入；报告提交前解析、LaTeX准备探测和工程报告已接入锁外请求归属；runtime._wsl_probe及Python依赖探测已按用户所属请求接通（case为空）；CLI discovery为文件发现，实际Agent/CLI原生执行归g | 统一同步/异步调用的所属范围、stdin/输出/期限与停止结果；线程被取消不能留下原生编译；保留独立复编译和发布校验能力 |
| N4-03.d SyncTeX | 注册与旧版路径已用私有冻结PDF/sidecar和请求所属WSL范围，原直接启动已移除 | 已验证HTTP断线/超时/登记失败/owner猝死及新API读取，保留历史映射和只读viewer；缺回执恢复与统一活动/停机继续N5/N4-07 |
| N4-03.e PTY | 正式路径已为NativeSession → OwnedTerminal → ConPTY/Linux PTY，根与实际gate登记后放行，同根重连 | 已由双库各44项及S3可控各9项验证；退出含实际脱离后代，2MiB背压与关闭互不阻塞。完整资源预算/活动/停机及缺回执恢复继续N4-05/07、N5 |
| N4-03.f viz | NativeVizSession → OwnedProcess，根登记后释放kernel，首帧出生身份和所属范围核验后active | 双库各33项及S3可控各9项通过，实际脱离后代/owner猝死、缺回执只读/不重放、copy/trash保护及双API端口竞争通过；完整跨主机路由、预算与N5恢复继续。关闭查看不取消工程Job |
| N4-03.g Agent/CLI | API shell、Windows/WSL CLI 持久会话/回合/审批、启动回执和聊天 UI 已接通；WSL 私有目录初始化也已进入登记后的同一原生 scope；不是普通 Job | 初始化专项9/双库各16/实际CLI10通过；保持工具与会话归属，完整部署和 N5 对账继续；不得自动重放已授权副作用 |
| N4-03.h 专业与全入口复核 | 专业入口登记拒绝、实际求解器/编译器/库后代和 worker 检查点中断已分批验证；WSL CLI 初始化与固定身份读探测均已补生命周期约束 | 继续剩余取消竞争/持久读取、FloTHERM 复制分支、旧 worker/条件回退及跨运行时边界；见 professional-process-faults.md 与 owned-control-readers.md |

原实施约束（LaTeX已按此交付，其余入口继续适用）：主执行器可以控制和等待子执行；身份、停止结果与父 attempt 关联；启动登记失败不得释放载荷；禁止仅在 Windows worker 内创建一个无法由父级核验的 WSL helper 后宣称已统一。LaTeX协议与证据已落地；表中其余入口仍须独立完成相同验收。

PTY/viz/Agent 属于会话，不强制伪装成工程求解 Job；也不能仅调用内存 `supervisor.cancel_case` 后看到注册表为空，就推断原生后代已退出。控制端失败、实际清理失败与用户请求状态须分别保留证据。

## 整项完成条件与后续边界

N4-03 仅在上述入口完成所属关系、放行前登记、停止/实际退出等待，并通过对应真实工具及取消/断线/owner 猝死等隔离回归后关闭。错误登记、停止并发、线程取消、重复关闭和外部无关进程存活须有证据；不能用固定 sleep 或信号发送成功替代退出核验。子执行状态还须可从持久记录读取；跨重启重领、恢复与安全重试继续由 N5 验收。

N4-04 继续解决取消与发布竞争的持久协议；N4-05 继续预算/资源/磁盘策略；N4-06 继续公平调度；N4-07 继续统一健康与停机。N3-07 的完整 MPI/多工况/UDF 适配器及全功能各自剩余项不因本批进程清理通过而完成。完整 V11/V12 等故障矩阵也保持实施中或待验收，以台账逐项记录为准。
