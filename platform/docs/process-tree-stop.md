# 进程树停止、退出等待与持久清理证据

## V09 三个阶段取消几何预览

修复前，启动门已创建时取消作业，提交预览的 HTTP 请求在文件存储和 S3 上均返回 500。启动操作把用户取消当作协程中断传给 HTTP 调用方。现在启动租约保留作业取消原因，清理等待门后由原投递返回已接纳的作业。原提交与取消响应均能指向同一次尝试。[文件存储反例](../../tmp/live-cli-startup-qq6gtpb3/live-result.json) · [S3 反例](../../tmp/live-cli-startup-c9_uaer_/live-result.json)

两组实测先用 CadQuery 发布边长为 1 的盒体，再在以下阶段取消生成边长为 2 的预览。

| 取消时点 | 实际观察 |
|---|---|
| 启动门已创建 | 原提交返回 200、原作业编号及 cancelled。门进程退出，几何业务代码没有启动。 |
| 几何程序及子进程运行中 | 从实际 Windows 句柄验证两个进程均退出，程序启动标记可从日志读回。 |
| 原生执行成功，正在收尾 | 核对退出码 0 和已生成场景的日志后取消。作业与尝试最终均为 cancelled。 |

三个阶段均检查取消事件恰好一次、候选发布转为 failed、正式发布指针和场景文件字节保持原值，账户运行任务与预留字节归零。[SQLite 与文件存储](../../tmp/live-cli-startup-2zfrgkcb/geometry-cancel-phases.json) · [PostgreSQL 与 S3](../../tmp/live-cli-startup-6of_04zl/geometry-cancel-phases.json) · [验收入口](../server/live_llm_geometry_cancel_phases_acceptance.py)

Windows成员观察修复：确认所属范围后，进程可能在读取出生时间前退出。观察器通过已保留的同一查询/等待句柄读取其不可变出生时间，再等待该对象退出并与Job累计成员数对账；不按PID重找或终止。对外observe/probe及启动登记仍默认拒绝已退出进程。新增定序真实进程用例验证该窗口，生命周期现12项，详见 [owned-pandoc-children.md](owned-pandoc-children.md)。

N4-03首批将MPI以nobody账户启动并保留非零UID进入私有namespace，真实ranks停止双库各6项通过。内部编译及交互旁路仍待接通，详见 [native-entry-supervision.md](native-entry-supervision.md)。

日期：2026-11-02。N4-02已完成通用执行范围的停止与退出核验；N4-03继续接通其余原生执行入口。N4-01 的出生身份与登记前启动门继续有效，见 [process-identities.md](process-identities.md)。

## 本批行为

通用 Job 执行器现在同时观察宿主根进程退出和日志管道。根进程退出而后代仍持有 stdout 时，执行器会收尾本次归属范围，然后排空日志；不再只依赖 stdout EOF 才开始清理。用户取消、墙钟超时、日志限额、所有权丢失、执行异常与正常根进程退出复用一个幂等停止操作。

Windows 启动门使用隐藏控制台，其用户命令创建独立进程组。停止时先向这个组发送 CTRL_BREAK，给协作进程退出机会。用户取消默认宽限 2 秒；墙钟/所有权/执行异常通常 0.5 秒；日志超限及正常根进程退出后的残留收尾为 0。直接 shell 先退出时，剩余成员仍享有本次剩余宽限。

宽限结束后，Windows Job Object 设置 `ActiveProcessLimit=0`，封住新进程关联，再读取该 Job 的成员列表。对成员只打开查询/等待句柄，复核 Job 归属；PID 已复用且不属于此 Job 时关闭该句柄，不发送信号。随后通过原持有的 Job Object 结束剩余成员，等待已保留的成员句柄真正退出及 Job 清空。成员观察上限 4096；观察/等待失败不能登记为已验证清理完成。

这是清理阶段的封闭措施，不是已经实现全部 CPU、内存、磁盘或每用户进程预算。测试中发现 Job 计数清零与个别进程句柄进入已退出状态存在时间窗口，因此不能用计数代替句柄等待。[TerminateProcess](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-terminateprocess) 明确要求对异步终止使用进程句柄等待；Job 终止具有相应语义，见 [TerminateJobObject](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-terminatejobobject)。新增关联限制的依据见 [Job basic limits](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_limit_information)。

WSL 继续使用独立的 user/mount/PID namespace。监督进程通过仅传给 namespace init 的控制 FD 转发停止请求；init 向其未回收直接子进程所属组发送 TERM，超时后升级为 KILL。监督进程另有外层期限，必要时通过持有的 pidfd 结束 unshare 子进程。init 退出会关闭这个 namespace 的其余后代，包括脱离原 group 的孙进程。控制管道 EOF 也触发限时收尾；真实 owner 猝死的退出链已经测试。

## Windows生命周期成员核验

在首个进程分配前，为本次Job关联私有I/O completion port并启动有界观察器。创建通知只用于打开查询/等待句柄，随后复核Job归属与真实出生时间；从不据通知PID执行终止。已退出句柄由真实等待信号确认后释放，即使该成员在最终PID列表中已经消失，其退出计数仍被保留。

Windows不保证每条成员通知送达，且通知PID可能已经复用，参见 [Job completion port](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_associate_completion_port)。因此收尾封闭新成员、结束剩余成员并等待范围清空后，还必须用 [累计TotalProcesses](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_accounting_information) 与已观察的不同出生代次对账。重复通知不能补足缺失成员；对账不全会超时失败，不能仅凭ACTIVE_PROCESS_ZERO通知宣布完成。

若通知到达时OpenProcess明确返回进程对象已不存在，记录为打开前已消失；同一PID再次出现但缺少足够出生信息时保守失败。打开的对象不属于本次Job时，只有其等待句柄已证明退出才可算消失；存活的外来代次不能算清理证据，也不会收到信号。

观察器最多保留65536个生命周期代次、4096个未核实退出句柄；超过界限、权限/等待错误或通知缺失均会令清理核验失败。这是观察器的有界保护，不是完整CPU/内存/用户配额。成员创建失败也可能增加累计数；无法对账时保留失败，不能臆测它未运行而扣除计数。

停止记录新增可选`scope_observation`：`schema/total/observed/exit_verified/gone_before_open/pending/complete/error`。`complete`要求总数相等、pending为0且无观察错误。旧记录保持可读，缺字段不代表具有新核验证据。记录是停止当时的快照，失败后的实际退出不会反向改写历史为成功。

封闭新成员失败也进入持有Job的后备终止。若连Job计数读取都失败，通用停止操作仍通过已持有对象请求终止、等待根进程，保留原始失败；后备请求不使verified变为true。WSL身份探测未知同样不能被改写成已退出。

## 控制消息与日志分离

固定启动门的 stderr 是独立、受限的完成报告通道；用户命令的 stderr 显式并入原 stdout 日志。普通日志即使含 JSON 文本也不能冒充完成报告。

控制消息包含 nonce、角色、停止请求是否收到、温和请求是否转发、是否显式升级及直接子进程退出码。接收端限制为 8192 字节、固定字段和角色，拒绝重复角色、错误 nonce 或未知字段。未见必要的固定完成报告时，原生命令退出码 0 也不能被登记为成功。

`forced` 表示观察到的显式强制结束操作；它不保证其余成员都执行了用户层清理回调。特别是 namespace init 退出本身会结束剩余 namespace 成员。公开的 `verified` 表示这次执行器已按本页描述完成归属、句柄/范围和适用 native 退出核验，不是任意外部进程的全机无残留声明。

## 持久结果与 API

`GET /api/v1/jobs/{id}` 新增 `attempt.process_stop`。没有已提交结果时为 `null`；原有取消接口的 `cancelled` 仍表示取消已接受，不能将其当作清理已完成。

结果采用 `vibeflow.process-stop.v1`，包含：

| 字段 | 含义 |
|---|---|
| `launch_nonce/reason` | 绑定实际启动与第一项停止原因；重复停止复用同一次操作 |
| `grace_ms/request_sent` | 配置宽限及父进程控制管道是否接受请求；不等于用户程序已响应 |
| `forced/scope_empty/native_state/root_exit_code` | 强制结束、所属范围清空、WSL 身份退出观察及原生根退出码 |
| `control_valid/control` | 固定启动门报告是否合法及其有界记录；不包含用户 stderr 原文 |
| `verified/error` | 清理核验是否通过及异常类型；失败不会变成成功 |
| `started_at/completed_at` | 本次停止过程的 UTC 时间 |

结果绑定当前 claim/attempt 与已登记 nonce，写入现有 `output_manifest_json.process_stop`，与最终状态及 `attempt.process_stopped` outbox 事件一同提交，没有新增数据库迁移。字段格式损坏或跨启动结果不能公开为有效证据；已有清理结果不能被不同值覆盖。

新增错误码 `process_cleanup_failed`。原本可成功结束的命令若清理无法核实，会以失败结束并保留证据；已接受取消的作业保留现有cancelled兼容状态，同时持久保存verified=false及错误，不能将取消响应当作清理成功。该码已进入状态机允许集合；故障注入曾揭示遗漏错误码会把任务卡在 `finalizing`，补齐后双库测试通过。

DB 终态提交之后，本进程可能仍在执行必要的失败回退等收尾；因此测试等待终态、`ended_at` 与清理结果，而不是仅看到一个 JSON 字段便认为整个 Job 已返回完成。跨 API 与读取端重启会读取相同持久结果。

## 首批证据

首批汇总：`tmp/mainline-N4-tree-evidence`。全部 API、数据库、存储和进程均由隔离测试创建，开发库不迁移、开发 API 不重载。

- `owned_tree_smoke.py`：10 项。Windows/WSL 各验证协作退出、忽略信号、脱离 group、根进程先退出，以及真实 owner 解释器猝死；先观察具体后代出生身份，再核对退出，两个外部哨兵存活。
- `process_tree_smoke.py --postgres`：双库各 13 项。真实 Windows shell/WSL 经 API 验证取消、升级、后代、根退出、墙钟/日志限额及持久结果；故障控制报告不能造成成功。重复 HTTP 取消仍保留现有响应语义，未将其宣称为 N4-04 的最终幂等接口。
- `process_registration_smoke.py --postgres`：双库各 20 项复验，额外验证两个 API 读取进程及读取端重启后的清理记录一致。
- 收尾/状态/调度/认领/封存、core、execution 及已选择的注册处理器回归，以汇总目录中的实际结果为准；未运行的全集不能由这些结果代替。

## 补充竞态与真实编译器验收

`owned_tree_races_smoke.py` 最终16项通过，证据 `tmp/mainline-N4-tree-races-evidence/owned-tree-races-result.json`。本批仅增加测试和文档，生产代码逐项匹配前批730文件基线，因此保留前批双库/兼容结果的实际时间，不将它们称为本批重跑。

- Windows/WSL 各6项：收到停止请求后再派生后代；慢清理回调正常完成；Windows独立隐藏控制台/WSL独立会话；调用者被连续取消仍等到清理结束才抛取消；控制EOF；非法控制消息触发限时收尾。每次捕获实际存活后代的出生身份，再核对退出，外部哨兵继续存活。
- Windows成员查询拒绝1项：仅在隔离测试对象注入权限错误，原持有Job执行后备终止，已观察后代随后退出；停止结果保持`verified=false`及错误，不将异步终止请求冒充已验证完成。
- WSL监督器猝死1项：在所属发行版双次复核本次出生身份，打开并仅通过pidfd结束指定监督器；验证namespace后代退出，不使用全局PID杀进程。
- 真实编译器2项：Windows Pandoc在Lua转换回调内、WSL latexmk/pdflatex在TeX展开循环内停止。由实际编译器立即写入并关闭标记文件，确认其仍存活且尚无产物，再取消并核对所观察进程退出。这是编译中途，不是编译后暂停。初版TeX stdout标记受缓冲影响导致等待超时，改用上述直接文件标记后最终通过。

这些用例补齐了首批列出的具体场景；该批结束时，高频短命成员核验仍未完成，随后由下方最终生命周期验收补齐。N4-03全部编译/交互旁路仍待接线。

## 最终生命周期与故障验收

汇总 `tmp/mainline-N4-lifetime-evidence`。专项11项通过：清理前已退出的64个短命子进程、停止期间持续派生、延迟到对象消失后才处理通知、缺失通知加重复通知、观察权限拒绝、外来存活代次、封闭成员失败、最终成员打开失败、Job计数失败、句柄等待超时、WSL身份探测未知。正常场景总数/不同代次/退出一致；故障场景保留失败及证据，已观察后代退出，外部Windows/WSL哨兵存活。

API双库各15项额外验证缺失成员通知后即使命令退出0也以process_cleanup_failed结束，并持久保存未完成的累计对账结果；已接受取消但清理未核验时继续保留cancelled及失败停止记录，不能登记成功。登记双库各20项复验跨API读取一致。core14、execution3、契约7、注册处理器4（LaTeX、普通报告、后处理、几何）及注册求解1通过。具体套件起止和最终失败分支复验顺序在evidence-origin.json；未把历史测试当成本轮重跑。

N4-02已按通用执行范围验收，原剩余检查单已由以上实现和实际证据补齐。

## 后续入口接线与边界

已经属于本次Windows Job/私有WSL namespace的进程由上述机制负责；经外部服务创建、独立交互会话或尚未接线的内部WSL调用，不因本批完成而获得该归属证明。完整V11与V12等跨入口故障矩阵继续随N4-03之后验收。

Windows worker 内部自行启动的 WSL 编译器、API 最终复编译、MPI ranks、PTY/viz 的全部旁路接线按 N4-03 继续。持久取消竞争、跨 worker 取消和最终 HTTP 幂等响应按 N4-04 继续；完整预算/队列/关闭/失联恢复分别留在 N4-05/06/07 与 N5，295 项全功能交付范围不缩减。


## V11 Windows 编译作业取消

2026-11-18，通过公开作业 API 在两个独立用户的算例里并行运行两棵进程树。首个算例的子 shell 启动 Python，Python 启动原生 TypeScript 7.0.2 编译器并持续监视输入。读到生成的 JavaScript 后，第二个用户尝试取消该作业收到 404；随后所有者成功取消。对照算例中的 Python 和 Node 在此期间保持运行。

每个进程先记录 Windows 出生身份，再持有只读查询和等待句柄，并从句柄校验可执行文件名。取消后编译进程树的三个句柄均已退出，生产停止回执显示范围已清空，账户运行中作业数为零，原始 TypeScript 文件保持一致。对照进程在取消后接收新生成的随机数，计算 SHA-256，返回正确结果并正常退出。两组分别使用 PostgreSQL 配合文件存储及 SeaweedFS，每组各有两次真实 DeepSeek 请求读回验收结果。[文件存储](../../tmp/live-cli-startup-onnuotbq/compiler-cancel.json) · [S3](../../tmp/live-cli-startup-t78bu8fz/compiler-cancel.json) · [验收入口](../server/live_llm_windows_compiler_cancel_acceptance.py)
