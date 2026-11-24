# 持久进程身份与登记前启动门

N5-01/N5-02首批：新claim持久保存真实worker出生身份，启动按case锁逐项对账；活owner（含过期租约）与未知归属不再被批量中断。精确退出owner仅在claimed/已登记running记中断审计，不重放、不伪造停止回执，starting/finalizing保留待恢复。双库专项各21、迁移各166、兼容6项原通过+终态专项双库各9通过；新增head4e2da9f3c865，仍35表，开发库未动。N5两项转实施中；53/295范围不变，主线11完成/18实施中/24待实施。 详见[Job实例身份与启动对账](job-owner-startup.md)。

N4-04：双API实际复现并修复取消已提交后仍放行命令的窗口；最终放行与取消共用case数据库锁，重验claim/worker/租约/实际登记。登记函数已返回后的启动失败保存所属停止回执；双库各8项（Windows/WSL、取消前后、通知失败、过期、放行后提交失败）及定向兼容7/7通过。53/295、11完成/16实施中/26待实施及head3d1c98e2b754/35表不变。 详见[取消与启动门顺序](job-release-cancellation.md)。

N4-03.h：复现并修复API退出后WSL只读身份探测残留；身份/ancestry改用所属Windows范围、固定Linux读程序、10秒原生期限及管道退出。专项11（取消0.328秒/API猝死0.671秒/期限10.532秒）、登记双库各20、PTY双库各44、CLI/基础兼容5/5通过（实际Linux CLI10、准备9、传输7）。不创建业务Job，不改变原持久登记后放行；53/295、11完成/16实施中/26待实施和head3d1c98e2b754/35表不变。 详见[所属只读身份探测](owned-control-readers.md)。

Windows成员观察修复：确认所属范围后，进程可能在读取出生时间前退出。观察器通过已保留的同一查询/等待句柄读取其不可变出生时间，再等待该对象退出并与Job累计成员数对账；不按PID重找或终止。对外observe/probe及启动登记仍默认拒绝已退出进程。新增定序真实进程用例验证该窗口，生命周期现12项，详见 [owned-pandoc-children.md](owned-pandoc-children.md)。

N4-03增量：固定WSL argv支持明确的nobody profile；放行前要求原生UID非零，登记时同时核对提交发行版。沿用现有身份schema与持久停止记录，MPI双库各6项和通用登记双库各20项通过，详见 [native-entry-supervision.md](native-entry-supervision.md)。

日期：2026-11-02。对应 N4-01。总体交付范围仍为 53 项主线和 295 项功能；本页不代表 N4 的完整取消、资源预算或 N5 的失联恢复已经完成。

## 已接入的路径

所有 `jobrunner.start_job` 调用先领取持久 attempt，再启动受控辅助进程。覆盖显式 argv、Windows shell、WSL 和 WSL sandbox，既有几何、网格、求解、后处理和报告处理器继续复用这个入口。普通 Job 的执行规格、日志路径、退出码、发布围栏和幂等请求保持原契约。

这里的 PID 是本次启动的宿主辅助进程和 WSL 监督进程，不冒充求解器的直接 PID。用户命令及其后代在该归属范围内启动；Windows worker 内部自行创建的 WSL 编译器、API 收尾时的独立复编译、PTY/viz 等旁路仍须在 N4-03 接入。

## 身份数据

现有 `JobAttempt.process_identity_json` 保存 `vibeflow.process-identity.v1`，没有新增数据库迁移。

| 字段 | 含义与验证 |
|---|---|
| `case_id/job_id/attempt_id/worker_id` | 由持有 case 锁的服务从数据库绑定；不能由 HTTP 调用方提供 |
| `launch_nonce` | 每次原生启动独立生成的 64 位十六进制随机标识；不是 claim 凭证 |
| `runtime/pid` | 已领取执行规格中的 runtime；PID 与宿主身份一致 |
| `host` | OS 身份 schema、平台、散列宿主标识、PID；Windows 的真实创建时间使用十进制字符串，避免 JSON 数值精度丢失 |
| `native` | WSL 发行版和 Linux 身份：boot UUID、start ticks、PID、process group、session、UID、PID namespace inode；非直接 WSL 路径为空 |
| `scope` | 本次新建的 Windows Job Object 名称，或 Linux pidfd 所属方式；不接管已有同名对象 |
| `gate_sha256/identity_probe_sha256` | 当前固定启动门与身份探测模块摘要；登记时复核，源代码改变则拒绝放行 |

Windows 标识来自操作系统 MachineGuid 的单向散列。Linux 优先使用有效 machine-id；本机 WSL 的 machine-id 尚未初始化，因此退回带独立命名空间的 boot UUID 散列。这个退回值会随重启变化，旧记录由此不能匹配重启后的进程。

登记时同时复核当前 worker、claim token、活动 attempt、执行规格与出生身份。完全相同的重复登记不增加版本或事件；不同 nonce、不同身份、外来 worker、过期 claim 不能替换原记录。已结束记录仍可作为历史读取，不会因此获得进程控制权。

## 放行顺序和故障处理

1. 在创建任何原生进程前提交 claim 与 `starting`。
2. Windows 使用基础 Python 解释器加 `-I -S` 启动固定辅助模块，保留创建时实际句柄。不能使用 venv 的 `python.exe` 重定向器充当被分配对象：重定向器可能先创建真实解释器，导致随后分配的 Job Object 无法覆盖该后代。
3. 辅助进程发送 nonce、角色及 OS 出生身份。父进程复核创建句柄/PID，将其分配给本次新建的 Job Object，确认归属后继续。辅助进程尚未运行用户命令。
4. WSL 采用两次放行。第一次只运行固定的 Linux 启动门；独立探测器在指定发行版内复核其出生时间、boot、session 等，再等待第二次放行。用户命令在私有 user/mount/PID namespace 中执行；namespace init 保持存活到直接命令结束。
5. `phase(running, identity=...)` 在同一事务提交绑定身份、attempt 状态、Job 状态和相关事件。该调用成功返回后，才向用户命令所在的启动门发送放行消息。
6. 握手、归属、身份复核或持久提交失败，关闭本次控制管道并清理持有的对象，不执行用户命令。取消发生在系统创建进程而异步接口尚未返回的窗口，也必须等到实际句柄被保存后再清理。
7. 日志泵等待进程结束、范围清理与原有关键收尾后，才提交终态。启动前后的取消分别保留空身份或已提交身份；极快退出命令保留其真实退出码与日志。

Windows 当前清理使用本次持有的 Job Object；Linux 使用未回收直接子进程的 pidfd 和控制管道。读取历史记录不会打开具有终止权限的 PID 句柄。Windows Job Object 的后代归属和嵌套约束参照 [Microsoft Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects) 与 [Nested Jobs](https://learn.microsoft.com/en-us/windows/win32/procthread/nested-jobs)，并以本机真实进程测试验证，不能仅根据 API 存在推断可用。

## 查询接口

`GET /api/v1/jobs/{id}` 的 `attempt.process_identity` 返回经 schema 和数据库归属验证的持久身份；未登记、旧格式或无效记录返回 `null`。原有 Job 字段保持兼容。

`GET /api/v1/jobs/{id}/process` 执行只读身份观察，返回 `job_id/attempt_id/identity/state/reason/host/native`。两处查询都先检查 Job 所属 case 的读取权限。

| `state` | 含义 |
|---|---|
| `match` | 当前宿主及适用的 WSL 身份均与记录一致 |
| `exited` | 对应进程已退出；WSL 路径要求宿主与 native 探测均确认退出 |
| `mismatch` | 宿主、出生时间或范围身份不符；不会对这个 PID 发信号 |
| `unverified` | 旧记录、格式损坏、权限/探测失败、发行版未运行，或宿主与 native 退出结论不一致 |

宿主不符或无法核实时，不进入本机同名 WSL 发行版探测。探测前检查已运行发行版列表，不故意启动已停止的发行版；列表查询与后续 WSL 调用之间不是系统级原子操作，该并发窗口仍须在后续恢复策略中保守处理。查询只反映观察时刻，不能替代控制操作前的句柄/归属核验。

公开身份不包含 claim token、环境变量、完整 argv 或私有命令参数。未知扩展字段会令整个身份记录不可公开，避免把损坏记录当作任意 JSON 透传。

## 隔离验收

证据汇总目录：`tmp/mainline-N4-identity-evidence`。最终回归和基线状态见该目录中的汇总及 [主线台账](mainline-task-status.json)。

- `process_identity_smoke.py`：Windows 5 项、真实 WSL 5 项，覆盖出生时间、宿主/boot/namespace/group/UID 不符、句柄归属、同名 scope 拒绝、退出及外部存活哨兵。
- `owned_process_smoke.py`：8 项，覆盖基础解释器与 venv 用户命令、未放行断开、错误 nonce、创建窗口取消、WSL 双门、native 探测失败和控制管道关闭。
- `process_registration_smoke.py --postgres`：SQLite/PostgreSQL 各 20 项，覆盖持久登记前后门禁、重复登记、提交回滚、错误出生标记/宿主/源码摘要/发行版、登记前后取消、三种 shell 路径、快退出、两个真实 API 读取进程及读取端重启、旧/损坏/跨 attempt 记录和权限。
- `job_claim_smoke.py --postgres`：双库各 13 项，复验两个独立 worker 竞争只启动一次，领取失败与过期回调继续受阻。
- 既有最终收尾、注册网格、核心 API、封存输入、几何/后处理及报告编译回归另存本轮汇总，不将上一轮结果标成本轮重跑。

数据库、API 端口、存储与运行时配置均为测试创建。PostgreSQL 只创建并删除名称受限的本地临时库；不迁移或重载开发库/API。测试只处理自己创建的进程和句柄，保留失败日志与证据。

## N4-02实施进展与后续顺序

N4-02的应用接线、持久清理记录及生命周期/故障验收现已完成，详见 [process-tree-stop.md](process-tree-stop.md)。下面保留预检来源与历史检查单；当前下一项是N4-03入口接线。

N4-02：以这些身份和持有的对象实现温和停止、限时升级、真实退出等待和残留检查。重点验证主进程已退出但后代仍持有 stdout、脱离进程组的后代、Windows 与 WSL 启动器突然退出、TERM 被忽略等窗口，区分用户取消与清理失败。

已有一项受限预检实际复现：宿主启动门返回 0 后，其后代仍能保持 stdout 打开；直接调用本次 owned scope 的收尾可结束该后代，外部哨兵保持存活，见 [输出生命周期预检](../../tmp/owned-output-lifetime-13n49b2y/result.json)。因此 N4-02 必须让日志泵同时观察根进程退出，不能只等 stdout EOF；该窗口随后已在N4-02首批执行器和API测试中修复并验证。

Windows 温和停止的另一项 [隐藏控制台预检](../../tmp/hidden-console-stop-ijm9hrly/result.json) 证明：自建隐藏控制台中，向本次新建且仍存活的进程组发送 CTRL_BREAK，目标协作进程退出，同控制台的另一组哨兵存活。[第一版](../../tmp/hidden-console-stop-ep243ulu/result.json)让 Python 一次 sleep 20 秒，发送请求后仍未在 5 秒内退出；改为短轮询后通过。这说明停止请求成功不等于退出，必须限时升级。随后已接入固定启动门、持久停止结果并验证忽略信号；第二批也验证了新建隐藏控制台、晚派生和慢清理，剩余成员退出窗口见process-tree-stop.md。

相关系统约束：`CREATE_NO_WINDOW` 不提供控制台句柄；`CREATE_NEW_PROCESS_GROUP` 与 `CREATE_NEW_CONSOLE` 同时指定时前者会被忽略，见 [Process Creation Flags](https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags)。控制事件只能到达与发送方共享控制台的目标组成员，见 [Console Process Groups](https://learn.microsoft.com/en-us/windows/console/console-process-groups)。这些文档和预检用于选择 N4-02 的实现，当前固定启动门已使用隐藏控制台，用户命令再创建独立进程组。

N4-02 的具体实现检查单：

1. 并行观察根进程真实退出与 stdout 排空；根进程先退出时及时清理仍占用管道的本次后代，保留退出码且不提前发布。
2. 统一幂等停止操作，区分启动放弃、用户取消、预算触发和正常退出后清理；记录是否发送温和请求、是否升级及是否确认退出。
3. Windows 先在隐藏控制台中验证目标组的协作退出，再接入固定启动门；超时、脱离控制台或忽略请求时只通过持有的本次 Job Object 升级，不用 PID 枚举杀进程。
4. WSL 将限时停止请求送到私有 namespace 的 init，由其向本次任务发送 TERM；超时再用持有的对象结束 namespace。不能把向 unshare 启动器发送 TERM 当作已停止。PID namespace init 的退出与信号规则参见 [Linux man-pages](https://man7.org/linux/man-pages/man7/pid_namespaces.7.html)，仍须实测控制管道和父进程突然退出。
5. 增加真实父子/孙进程、脱离 group、忽略信号、stdout 悬挂、反复取消、主进程极快退出、worker 猝死及外部哨兵验收；保留 SQLite/PostgreSQL 终态和发布围栏验证。

WSL 的 [init 控制管道原型](../../tmp/native-init-control-mlhplhb1/result.json) 已通过协作 TERM、忽略 TERM 后升级、控制管道 EOF 三种场景。每次均只创建本次 namespace 与测试进程；脱离原 process group 的孙进程心跳停止、输出管道关闭，namespace 外哨兵存活。这验证了控制 FD 穿过 unshare 到达 init 的可行性，随后已接入固定启动门和持久停止状态，组合故障验收仍待续。

N4-03：将 Windows worker 内部的 WSL 编译器、API 独立复编译、MPI ranks、PTY/viz 纳入同一归属契约；交互会话断开和用户取消分别定义。

N4-04/05：持久取消请求与发布竞争、墙钟/日志/磁盘/并发预算和配额释放。N4-06/07：公平队列及关闭。N5 使用同一出生身份和 ownership 对账失联 worker；当前没有自动接管或仅凭 PID 恢复后 kill。
