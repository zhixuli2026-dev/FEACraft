# N4-03：准备请求的原生归属基础

N4-04：复现并修复父Job取消已提交后仍放行原生子执行；worker/发布复验最终发送与父取消同锁，准备请求复核所属记录与租约，本地关闭在最终发送前再次检查。专项双库各20（SQLite原11+定向复验9，PostgreSQL完整20）、编译/准备/Agent/runtime/SyncTeX/Job兼容8/8通过；夹具修正及原始失败日志保留。53/295、11完成/16实施中/26待实施及head3d1c98e2b754/35表不变。 详见[原生子执行放行](native-release-cancellation.md)。

N4-03.g2b2：宿主Codex原生回合/SSE、幂等消息、复杂审批/计划问答、显式线程续接及API/Goal/审批共用执行归属已接通。双库各34回合、8暂存、6双API及115迁移约束通过；旧CLI会话各34、Agent shell各32、core14/14、协议辅助7/7通过，真实CLI本地Responses fixture6项通过，无远端推理或个人登录。head f97e54a8d310、30业务表；开发库旧13表未迁移/重载。下一单元g2b3账户登录/登出、sandbox状态/设置、WSL原生CLI与完整UI，随后专业入口、N4/N5；53/295和11完成/16实施中/26待实施不变。详见 [owned-cli-turns.md](owned-cli-turns.md)。

N4-03.g2b1：宿主CLI持久会话和正式API已接通；两级实际登记后放行，私有账户/临时目录、账户/模型展示、唯一线程创建、case/S3收尾与历史保护通过双库各34项、S3可控各9、迁移各87约束，原Agent shell各32及core14/14通过。真实Codex经正式路由读取空账户/8模型、创建线程并核验关闭，无模型推理。head e86d43f7c209、26业务表；开发库旧13表未迁移/重载。下一单元g2b2持久回合/SSE/复杂审批，再登录/WSL/UI及专业入口、N4/N5；53/295与11完成/16实施中/26待实施不变。详见 [owned-cli-sessions.md](owned-cli-sessions.md)。

API Agent shell增量：ToolCall唯一NativeRequest，原子审批、实际根/来源/权限复验及停止核验通过双库各32项，S3可控各9项。head d75c32e6b1f8、25业务表；g2实际Codex/CLI及完整N4-03继续，详见 [owned-agent-shell.md](owned-agent-shell.md)。

viz增量已验收：NativeVizSession、放行前根登记、实际kernel首帧、整树退出/缺回执、持久历史和copy/trash保护通过双库各33项；S3可控各9项，本机双API端口竞争通过。head c64b21d5a0e7、25业务表，完整N4-03继续Agent/CLI及专业入口。详见 [owned-viz-sessions.md](owned-viz-sessions.md)。

PTY后续增量：NativeSession按用户/可选case保存根与实际gate出生身份、租约及停止回执；真实重连、退出/缺回执、2MiB输入与脱离后代清理通过双库各44项，S3可控各9项。代码head b53a10c4f9d6、24业务表；详见 [owned-pty-sessions.md](owned-pty-sessions.md)。

全局Python/WSL能力探测已接入按用户所属、case为空的请求范围，固定用途、实际退出与降级语义通过双库各25项；不分配Job。代码head ad42f9b3e8c5，仍23业务表，详见 [owned-runtime-probes.md](owned-runtime-probes.md)。

SyncTeX后续扩展：用途synctex.query/synctex_query已接入，允许固定只读查询而不创建Job；代码head9c31e8a2d7b4、仍23业务表，迁移35项约束及已有历史降级保护通过，详见 [owned-synctex-requests.md](owned-synctex-requests.md)。

后续业务整合已完成：正式报告/LaTeX及工程报告dispatch使用本服务，在case锁外执行解析/探测，锁内重验和幂等提交；详见 [document-admission.md](document-admission.md)。本文保留基础阶段的测试与设计顺序。

最终验证：请求SQLite/临时PostgreSQL各14项、5类损坏台账拒绝；迁移双库各34项约束/往返/metadata diff=0；正式Pandoc各19项、报告与LaTeX定向兼容2/2通过。754文件源码基线、原始结果及来源顺序见tmp/mainline-N4-request-evidence。

日期：2026-11-02。完整目标仍为 53 项主线和 295 项功能，N4-03 保持实施中。基础批次建立独立请求归属服务及隔离验证；随后正式报告／LaTeX及工程报告已按下文拆分接入，业务验收见上方后续记录。

## 请求与作业的边界

准备阶段需要解析 Markdown 或探测 LaTeX 工具链，此时参数、输入及工程条件可能尚不满足提交要求。`native_requests` 保存这类请求的原生执行记录，不创建 Job、JobAttempt 或发布版本。它没有幂等提交键，也不预占正式作业的执行权；正式提交仍须先重放已有请求。

新增迁移 `8b20d7f1c6a3`，前置 `7a91c4e2d6b0`，增加一张表，代码现为 23 张业务表。每条请求包含发起用户、API owner 标识、owner 的真实 OS 出生身份、私有 token 摘要、独立 launch nonce、固定 profile、心跳／租约、子执行台账及终态；文档请求另有 case，全局 runtime 请求要求 case 为空。数据库约束限定用途、范围、状态、非负版本，以及活跃记录没有结束时间、终态必须有结束时间。

请求 token 和 broker 能力凭据仅保留在所属执行器的内存中；表中只有 token 的 SHA-256。内部只读投影不返回这些凭据。现有开发数据库仍保持旧版本，本批迁移验证只使用独立 SQLite 与命名临时 PostgreSQL；没有迁移或重载开发实例。

## 执行与持久记录

`native_request_execution.scope → RequestExecutor.run_sync → native_execution_client → NativeExecutor → OwnedProcess`。

执行器复用正式子执行的 localhost 私有通道、启动门、保留句柄、Windows Job、WSL 私有 namespace 和实际退出核验；台账写入改由 `native_request_records` 处理。文档请求接受 `report.prepare/pandoc_parse`、`latex.prepare/latex_probe` 与 `synctex.query/synctex_query`；后续全局请求另接受 `runtime.python_probe/python_runtime_probe` 和 `runtime.wsl_probe/wsl_runtime_probe`，只绑定用户、case必须为空。各类请求均不能调用其登记用途之外的文档编译、Pandoc 版本查询或任意 argv。

顺序为：提交请求 owner；提交 child preparing；创建等待放行的所属进程；独立核验 host／native 出生身份与启动门源码摘要；提交 child running；释放工具。每次 JSON 变更先用 owner、token 摘要、nonce 和 active 状态执行受条件约束的 UPDATE，串行化该请求的台账写入。这里不取得 case 控制锁。

登记既检查探测前的租约，也检查出生身份探测结束时的租约。若探测期间租约过期，工具不会被放行。每 5 秒续租一次，租约 30 秒；续租失败封闭通道并停止所属子执行。请求不会因 API 重启而自动被另一 owner 领取。

子执行完成后保存原始 OS 身份、停止原因、实际退出核验与错误类别。同步调用使用 ContextVar 向线程传递所属通道；线程取消或等待超时会先封闭 broker、停止并等待原生范围，再等待同步线程结束。正式HTTP接口现已在业务整合中把请求断开转换为这个取消路径。

请求成功要求所有子执行都是 stopped，且各自具有 verified 停止回执。操作失败和清理是否核验分别表达；回执提交失败不能凭内存执行器为空而产生成功记录。API 猝死后，操作系统范围清理真实后代，数据库保留缺少停止回执的原记录；租约过期只投影为 recovery_required，不补造 ended_at、stop 或成功。

内部只读 `public` 投影用于检查服务和隔离验证，当前没有新增面向产品的请求查询／取消路由。损坏台账返回 evidence_invalid，不把未知清理状态转换为已核验。

## 本批验证

`platform/server/native_request_smoke.py --postgres` 在独立 API、文件目录、SQLite／临时 PostgreSQL 上运行，使用真实 Pandoc 和 WSL LaTeX 探测。所有测试路由、故障开关和定序屏障均在临时 app 中。

覆盖正常调用、限定用途拒绝、取消、工具期限、实际控制连接断开、错误出生身份、错误 nonce、探测期间租约过期、登记提交失败、停止回执提交失败、清理证据不足、续租失败及 API owner 猝死。检查真实工具出生身份已退出、无关进程仍存活、工具运行时另一个 case 写入可完成、错误 owner 被拒绝、Job 数量不增加，以及新 API 逐条读取原台账。

另拒绝请求 nonce、用途、runtime、停止 nonce 和 owner 出生身份五类损坏记录。新增迁移由 `workflow_migration_smoke --postgres` 检查双库约束、旧 Job 保留、升级／回退／重升和 Alembic metadata diff。最终通过数量、原始运行路径及兼容结果记录在 `tmp/mainline-N4-request-evidence/`，以该处实际结果为准。

## 业务整合清单（1—5已接入并验证，继续6及完整N4/N5）

1. 保留第一次幂等重放在任何 mutable source、工具探测和昂贵解析之前。捕获授权用户、case revision、工程发布指针及输入选择，释放 case 写事务和连接。
2. 在独立准备空间冻结将要解析的源字节和资源选择；通过本服务执行 Pandoc／LaTeX 探测。解析出的所有依赖必须属于冻结来源，不能混入准备期间被改写的资源。
3. 回到短写事务后再次查幂等键，处理两次并发相同提交只产生一个 Job。重验 revision、文件哈希、当前工程发布条件及权限，再原子封存正式输入、提交 Job 并绑定发布；来源变化返回冲突。
4. 将 `ReportHandler.prepare`、`LatexHandler.prepare` 拆为选取、原生准备和锁内重验，接通 `reports.py` 与 workflow dispatch。工作流额外重验 run／step version 和 ready 条件，准备失败或失效不能留下占用步骤的无效 Job。
5. 通过正式端点验证准备时并发写入、同键双提交、输入删除后的幂等重放、HTTP 断开、超时、错误登记和 owner 猝死。现有隔离服务测试不替代这些业务验收。
6. 再扩展 runtime／CLI 探测与 SyncTeX，只允许各自固定 profile；随后继续 PTY、viz、Agent／CLI 与其他入口。累计预算、配额和请求记录保留／回收接 N4-05/06；统一停机接 N4-07；持久恢复与条件化补偿接 N5。

实现：[请求执行器](../server/app/services/native_request_execution.py)、[请求台账](../server/app/services/native_request_records.py)、[迁移](../server/migrations/versions/8b20d7f1c6a3_native_request_scopes.py)、[专项](../server/native_request_smoke.py)。此前正式作业子执行与终态协议分别见 [Pandoc](owned-pandoc-children.md)、[LaTeX](owned-native-children.md) 和 [终态投影](job-terminal-projection.md)。
