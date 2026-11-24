# N4-03：LaTeX 原生子执行归属

后续SyncTeX只读请求也已接入锁外NativeRequest，历史映射/旧版来源重验、HTTP断线与实际退出、viewer权限复核通过双库各25项；继续其余探测和会话入口，详见 [owned-synctex-requests.md](owned-synctex-requests.md)。

后续正式整合：报告/LaTeX提交及工程报告原生准备已移出case写事务；HTTP断线、幂等和来源重验通过本批隔离验证，详见 [document-admission.md](document-admission.md)。原有正式作业、独立内容验证与发布协议继续适用。

后续增量：独立准备请求归属基础及双库各14项验证已落地，正式业务锁外准备、来源重验和HTTP断开仍在下一单元接通；见 [native-request-scopes.md](native-request-scopes.md)。本页正式作业协议继续适用。

后续LaTeX专项两库各15项新增核对本机/新API的逻辑退出码和数据库结束时间；成功发布兼容文件尚在收尾时由持久标记统一等待，见 [job-terminal-projection.md](job-terminal-projection.md)。

后续Pandoc解析/编译/版本及API复编译已接入相同父级归属协议，按purpose区分host与WSL身份；finalizer同步连接失败后的残留调用也会在父终态事务前等待。双库各16项及成员出生读取竞态修复见 [owned-pandoc-children.md](owned-pandoc-children.md)。

最终验收：原生子执行SQLite/临时PostgreSQL各15项，包含真实Windows无符号退出码和ownership错误；两库均逐项从新API读取原台账，并拒绝跨用户读取。迁移双库各27项约束、发布竞争各10项、core14/14、execution3/3、收尾与LaTeX2/2（LaTeX32项）通过。Windows退出码修复后另重跑execution3/3、收尾1/1和双库退出码/ownership专项各2项；core、完整LaTeX及发布竞争在此前的子执行代码上通过，修复前后的时序见证据目录evidence-origin.json。

日期：2026-11-02。本文记录 N4-03.b/c 的 LaTeX 增量；主线 N4-03、N4-04 继续实施中。完整范围仍为 53 项主线、295 项功能，见 [主线计划](next-stage-mainline-plan.md)、[全功能计划](full-function-delivery-plan.md) 与 [入口监督清单](native-entry-supervision.md)。

## 问题和本批范围

注册 LaTeX 作业的外层 worker 是 Windows Python，内部通过 WSL 启动工具探测和实际编译。Windows 根进程退出，不能单凭它的退出码确认 WSL 编译器及后代已经退出。API 发布前还会独立复编译一次；这一轮发生在 worker 已经退出之后，同样需要归属、取消和退出核验。

现在这三类调用都交给拥有该 attempt 的 API 执行器创建：worker 工具探测、worker 正式编译、API 发布前独立复编译。每次调用建立独立 `OwnedProcess`，沿用 Windows Job、WSL 双启动门、原生出生身份核验和私有 PID namespace。它们是 API 控制的子执行，不依赖 Windows worker 替父级清理 WSL。

普通 Job 和其余专业工具仍沿用已有入口。提交前探测、报告 Pandoc、SyncTeX、PTY、viz、Agent/CLI 的剩余工作在文末列出，不能由本批 LaTeX 验收推定全部完成。

## 启动与完成协议

1. 根 worker 启动前，API 创建只监听 `127.0.0.1` 临时端口的控制通道；worker 和 API finalizer 使用不同的随机能力凭据。
2. 根进程出生身份完成持久登记后，API 将根 launch nonce 绑定到子执行器，再释放 worker。
3. 子请求只允许注册配置中的发行版、recipe 和用途。编译配置必须位于当前 attempt 的 `.latex-compile-<uuid>/settings.json`，不得跨 attempt、通过链接跳转或改变 main、epoch、日志上限。
4. API 在短事务中核对当前 claim、执行 head、worker 和根 nonce，写入 `preparing` 及事件；随后准备尚未释放载荷的原生启动门。
5. API 独立核验 Windows/WSL 出生身份、发行版、启动门及探测器摘要，在新事务中提交 `running` 与绑定身份。提交成功后才能释放实际工具。
6. 独立观察根退出、输出、期限和停止事件。退出并完成所属范围核验后，持久保存停止结果，随后才向调用方返回。控制连接断开也会停止本次调用并等待清理。
7. 父作业成功发布前重新读取子执行台账。任何未完成、损坏、清理未核验或回执提交失败的记录，都阻止成功发布。

记录写入 `job_attempts.child_processes_json`，与输入快照和产物 manifest 分离，避免独立复编译更新子执行状态时误判产物快照被篡改。schema 为 `vibeflow.child-executions.v1`；每项包含 child UUID、purpose、caller、父 nonce、请求摘要、完整绑定身份、phase、停止结果、错误类别和 UTC 时间。请求参数只保存摘要；能力凭据不写入事件或公共结果。

`GET /api/v1/jobs/{id}` 在现有算例权限检查后返回 `attempt.child_processes`。损坏记录返回 `null`；未使用该协议的旧空值返回空台账，不能理解成所有入口都已监督。新 API 可读取历史记录，无需内存进程对象，也不会因此重放作业。

迁移 `7a91c4e2d6b0` 接在 `4c02b1a7d9e0` 后，仅增加非空 Text 列及 `{}` 默认值；仍为 22 张业务表。迁移、回退、已有 attempt 默认值在临时 SQLite/PostgreSQL 验证。开发数据库保持 13 张表（含 alembic_version）、旧 head `6fb6c18fb7ee`，本批没有迁移或重载开发 API。

## 取消、失败和 API 退出

根 worker 正常退出后，封闭 worker 凭据并等待其尚存子执行，API finalizer 凭据继续用于独立复编译。取消、预算停止、控制失败和 ownership 心跳失败则停止所有子执行。已持久接受的取消仍遵守 [事务外验证与取消协议](publication-preflight.md)，不能凭 HTTP `ok=true` 宣称清理完成。

`stopped` 表示执行范围退出核验成功且监督过程没有错误；工具非零退出码仍由调用方判定编译失败。`failed` 可以携带已核验的清理结果，例如超时后成功清理；也可以携带未核验结果。停止回执无法提交时，保留此前的 `running`，阻止发布，不能伪造持久完成。

双库故障测试还发现：Windows 原生退出码可能为 `4294967295`，直接写入 PostgreSQL 的 int32 列会导致父作业终态提交失败，SQLite 则能保存这个值。现在 Job/attempt 退出码采用相同 32 位模式对应的有符号整数（本例为 `-1`），原生停止证据保留未经转换的 OS 值。专项使用真实 Windows 异常退出及编译中 ownership 心跳故障验证收尾；没有扩大数据库字段或更改成功退出码。

API 猝死时，由它拥有的 Windows Job 与 WSL 父级死亡链负责终止本次执行范围。测试另行探测此前捕获的真实编译器和后代出生身份，确认它们退出。API 已死亡，数据库可能仍显示 `running`/`finalizing`；现有启动对账随后会将遗留活跃 Job 标记 `interrupted`，但不会生成缺失的子执行停止回执。基于实际所有者的恢复识别和终态补偿属于 N5，本批不把物理清理或这一旧状态对账冒充完整恢复。

## 验证

专项入口：`platform/server/native_execution_smoke.py --postgres`。使用独立 API、文件存储、SQLite 和命名临时 PostgreSQL；故障通过隔离 app 注入，生产配置不采用测试期限或故障开关。

专项覆盖真实 PDF 编译及三次子执行登记、worker/finalizer 编译中取消、子执行超时、日志超限、worker 提前退出、登记提交失败、错误父身份、错误原生出生身份、清理证据不足、finalizer 停止回执提交失败、ownership 心跳失败，以及 worker/finalizer 编译期间实际 API owner 猝死。另核验错误能力凭据/发行版、根退出后 worker 凭据失效、新 API 精确读取、跨用户拒绝、损坏台账拒绝、输入不变和无关 Windows/WSL 进程存活。

最终结果及兼容回归在本批证据目录 `tmp/mainline-N4-native-evidence/` 汇总；以实际结果文件为验收依据，未通过的场景不得标记完成。原始首轮测试曾因故障注入函数未正确替换而失败，该问题修正后重新验收。

## 后续任务与完成条件

| 剩余单元 | 要实现及验收的内容 |
|---|---|
| N4-03.b/c 其余同步调用 | LaTeX准备探测、报告Pandoc与独立复编译已接入；继续runtime/CLI等探测的明确所属范围、取消后的原生退出等待及锁外执行 |
| N4-03.d | SyncTeX已接通请求归属、实际退出等待与历史/只读语义；后续缺回执恢复及统一活动/停机继续N5/N4-07 |
| N4-03.e/f | PTY/viz 会话持久身份、启动登记、显式关闭/空闲回收、退出等待和 owner 猝死；断线重连不重复启动，也不误取消工程作业 |
| N4-03.g/h | Agent/CLI 和所有专业入口逐项审查真实原生调用；保持审批边界，完成真实工具及故障回归 |
| N4-04 | 取消、claim、启动门、提交、通知失败及重复请求的完整竞争矩阵；请求状态与实际退出分别记录 |
| N4-05/06/07 | 跨 worker/finalizer 的整次墙钟和累计输出/磁盘预算、配额释放、公平排队、统一健康与停机 |
| N5 | 原生已退出但回执缺失时的恢复、孤儿识别、重启后的发布/别名补偿和安全重试；不得自动重放用户操作 |

当前子执行限制为最多 4 个同时执行、16 个控制连接、128 条台账；探测 45 秒、编译 180 秒，输出有界。这些是本次调用的限制，不等于已交付整个 workflow 的资源预算。私有 PID namespace 用于生命周期归属，本批也不宣称实现任意 TeX 输入的通用安全沙箱。

实现入口：[native_execution.py](../server/app/services/native_execution.py)、[native_child_records.py](../server/app/services/native_child_records.py)、[native_execution_client.py](../server/app/services/native_execution_client.py)、[jobrunner.py](../server/app/services/jobrunner.py)、[专项测试](../server/native_execution_smoke.py)。
