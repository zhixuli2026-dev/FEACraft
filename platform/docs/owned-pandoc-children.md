# N4-03：Pandoc 子执行与标准输入

后续SyncTeX只读请求也已接入锁外NativeRequest，历史映射/旧版来源重验、HTTP断线与实际退出、viewer权限复核通过双库各25项；继续其余探测和会话入口，详见 [owned-synctex-requests.md](owned-synctex-requests.md)。

后续正式整合：报告/LaTeX提交及工程报告原生准备已移出case写事务；HTTP断线、幂等和来源重验通过本批隔离验证，详见 [document-admission.md](document-admission.md)。原有正式作业、独立内容验证与发布协议继续适用。

后续增量：独立准备请求归属基础及双库各14项验证已落地，正式业务锁外准备、来源重验和HTTP断开仍在下一单元接通；见 [native-request-scopes.md](native-request-scopes.md)。本页正式作业协议继续适用。

后续终态协议已区分逻辑退出码与原始OS码，并增加兼容产物收尾pending/settled。Pandoc专项扩展为双库各19项，真实验证写回屏障、别名失败、收尾回执失败与跨API重放/流；详见 [job-terminal-projection.md](job-terminal-projection.md)。

最终结果：Pandoc专项SQLite/临时PostgreSQL各16项，原有LaTeX各15项、发布竞争各10项、登记各20项、树各15项；Windows成员生命周期12项、身份Windows/WSL各5项通过。普通文档/注册工程报告各23项、core14/14、execution3/3、收尾通过。完整来源及修复前后顺序见tmp/mainline-N4-pandoc-evidence/evidence-origin.json。

日期：2026-11-02。本批接通注册报告 worker 和 API 发布前复编译的 Pandoc 调用。完整交付仍覆盖 [53 项主线](next-stage-mainline-plan.md) 和 [295 项功能](full-function-delivery-plan.md)，N4-03/04 继续实施。LaTeX 协议见 [所属原生子执行](owned-native-children.md)。

## 已接通的调用链

`registered_report_worker → report_build/report_artifacts → pandoc_execution → NativeExecutor → OwnedProcess → pandoc_native → Pandoc`。

worker 与 API finalizer 沿用不同的私有能力凭据。根 worker 的出生身份提交后才接收请求；子执行先保存 preparing，再核验实际 OS 出生身份、提交 running，随后才释放工具。根 worker 退出后封闭它的凭据，API finalizer 仍可调用独立复编译；取消及所有者错误停止全部所属调用。

| purpose | 固定行为 | stdin 与结果 |
|---|---|---|
| `pandoc_parse` | 注册 Pandoc 的 `--from=markdown --to=json` | 保留原 Markdown 字节，使用真正的 stdin；stdout 为 JSON，stderr 单独保存 |
| `pandoc_compile` | 读取本次 `document.json`，输出 `report.html` 或 `report.docx` | 固定 JSON reader、资源目录和 HTML 选项；不能请求任意 filter、shell 或输出位置 |
| `pandoc_version` | 注册 Pandoc 的 `--version` | 记录原始 stdout，保留 stderr |

普通 Markdown 报告成功构建有八条子执行记录：worker 解析一次、编译两种格式、版本探测一次；API 来源复核解析一次、独立文档解析一次、复编译两种格式。它们都绑定同一个父 attempt/claim/launch nonce，但具有各自的 OS 身份和停止回执。发布前继续核对来源、冻结文件、HTML 字节、DOCX 正文/资源及代次，原有发布条件没有由“进程退出”替代。

`attempt.child_processes` 的兼容 schema 增加上述 purpose，并按用途核验 runtime：LaTeX 仍为 WSL，Pandoc 为 host Windows 范围。子执行台账仍在已有 Text 列中，本批不增加表或迁移，代码 head 仍为 `7a91c4e2d6b0`。

## 标准输入与有界捕获

启动门的 stdin 只用于控制消息，不能直接转交给 Pandoc。适配器在当前 attempt 下建立独占 `.pandoc-call-<uuid>` 临时目录，保存原始 stdin 和固定配置。控制通道只传配置路径及 SHA-256；API 在启动前核对注册 executable、工作目录、用途、stdin 摘要和编译路径，原生 helper 在放行后再次核对配置摘要。登记后改写配置会失败，不能悄悄改变可执行文件或参数。

helper 是使用基础解释器 `-I -S` 启动的固定源码，不导入应用服务，也不使用 shell。它创建实际 Pandoc 进程，通过独立线程传入原始 stdin 并分别捕获 stdout/stderr。工具输出不经过启动门握手或控制 JSON；完成后才通过小型结果帧返回字节数与摘要。适配器核对结果文件的类型、大小和摘要，再恢复为 `CompletedProcess`，所以解析错误仍可得到真正的 Pandoc stderr。

每次调用 stdin/stdout 上限为 32 MiB，stderr 上限为 1 MiB；版本、解析和编译内部期限分别为 20、30、120 秒，外层监督另留 15 秒收尾余量。超限只操作创建时保留的 Popen 句柄，所属 Windows Job 最终核验全部后代退出。临时目录仅在本次调用结束后清理；HTML/DOCX 仍使用原有独立编译目录、冻结输入和不可变发布路径。

这些限制针对本次子调用的传输和输出。整个 workflow 的累计墙钟、报告产物磁盘配额、并发公平性及配额释放仍由 N4-05/06 验收。

## 断线、取消与终态提交

正常响应由 broker 在原生范围停止核验、回执提交后发送。控制连接断开则停止该请求，不依赖同步调用线程继续运行。父 worker 退出时会等待它的子执行；API 复编译返回后，还会封闭 finalizer 通道并等待残留调用，随后才进入父作业终态事务。这覆盖本地 socket 失败导致同步线程提前返回、而清理仍在进行的窗口。

专项测试使用清理屏障暂停实际停止：断开 finalizer socket 后，真实 Pandoc 仍等待 stdin，父 Job 必须继续 running 且没有 ended_at；释放屏障、核验进程退出后才允许保存失败终态。屏障用于制造竞态，退出结论由出生身份和所属范围核验给出。

清理未核验或停止回执提交失败均阻止成功发布。API owner 猝死后，Windows Job 会清理所属 Pandoc 和 helper；测试另外检查实际捕获的出生身份。现有启动策略可以将遗留活跃 Job 标为 interrupted，但不会生成缺失的停止回执，也不代表已完成 N5 恢复。

## Windows 短命成员竞态修复

发布竞争测试曾在一个正常报告的首次构建中失败：观察器已经用保留句柄确认该成员属于本次 Windows Job，但它在读取出生时间前退出，原 `windows_identity` 因“已退出”抛出异常。失败记录为 `process_cleanup_failed`，没有放行发布。

观察器现在仅在这条已确认归属的路径中，允许从同一保留句柄读取已退出对象的不可变出生时间，随后通过同一句柄确认退出，并与 Job 累计成员数对账。对外 `observe/probe` 和启动登记仍采用默认的存活检查。权限失败、陌生活跃进程、缺失消息和成员数不一致仍不能通过核验。

新增定序测试让真实子进程等到归属检查完成才退出，强制命中这个窗口。它核对退出前后的出生记录相同、默认对外查询拒绝已退出对象、观察器捕获该出生记录，以及三个实际成员全部退出；生命周期套件现为 12 项。原始失败证据保留在 `tmp/publication-preflight-bf3u3zcn/`，修复后的隔离验证在本批汇总目录中。

## 验证与证据

专项入口：`platform/server/pandoc_execution_smoke.py --postgres`，隔离 app 注入故障，使用独立文件存储、SQLite 和命名临时 PostgreSQL。结果汇总到 `tmp/mainline-N4-pandoc-evidence/`，以该目录实际通过结果为准。

专项覆盖：正常 stdin/HTML/DOCX、worker 与 finalizer 取消、期限、两类控制断线、父 worker 提前退出、登记 commit 失败、错误出生身份、放行前配置改写、清理证据不足、finalizer 回执提交失败、原始 Pandoc YAML 错误、实际输出限额、两阶段 owner 猝死、新 API 读取、跨用户拒绝、损坏台账拒绝、源文件不变和无关进程存活。

输出限额场景在隔离 helper 中将捕获上限设为 1 KiB，并使用真实 Pandoc 转换小输入，明确触发输出限额；生产配置仍采用上文限制。最初的大输入用例先触发期限，未把它计作输出限额通过。首次断线用例还修正了测试钩子的 caller 选择，避免把 worker 的正常收尾误停在 finalizer 专属屏障。

兼容回归包括 LaTeX 原生子执行专项、普通文档、注册工程报告、父作业收尾，以及 core/execution。各批 API 启动顺序和源码版本写在汇总证据中；此前通过结果不会冒充本轮重跑。

## 准备阶段已接通，继续其余入口

正式报告作业继续使用上述协议。提交前请求准备也已按下述1—4接通：处理器锁内选择字节，NativeRequest在锁外解析，回到锁内重验后才分配Job/attempt；LaTeX准备探测同样接通。1—4保留已验收要求，继续5中的runtime/CLI/SyncTeX及6：

1. 保留幂等重放优先：已提交请求必须先返回原作业，不能因当前源文件缺失、工具变化或昂贵探测而阻止重放。
2. 将原生准备工作移到 case 写事务之外，建立明确的 request owner、取消和出生身份记录；不能通过提前创建无效 Job 来改变“参数错误不创建作业”的契约。
3. 冻结用于解析的源字节、资源选择和相关工程指针；回到写事务后重验 revision、哈希及当前发布条件，再原子封存输入和提交作业。准备过程中发生变化应返回冲突。
4. 验证请求取消、线程异常、工具期限、登记失败、API owner 猝死和并发写入；不能以线程取消或 HTTP 返回代替原生退出核验。
5. 同步接通 LaTeX 准备探测、runtime/CLI 探测和 SyncTeX 请求，复用明确的请求归属，同时保持只读及历史映射语义。
6. 按 [入口监督清单](native-entry-supervision.md) 继续 PTY、viz、Agent/CLI 和其余专业入口；持久租约恢复、孤儿识别、发布补偿及安全重试继续 N5。

实现：[pandoc_execution.py](../server/app/services/pandoc_execution.py)、[pandoc_native.py](../server/app/services/pandoc_native.py)、[native_execution.py](../server/app/services/native_execution.py)、[专项测试](../server/pandoc_execution_smoke.py)。
