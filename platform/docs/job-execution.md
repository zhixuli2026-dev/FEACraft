# Job 提交幂等、原子领取与持久尝试

上传取消的线程越界已修复：创建/写块/完成/取消四个操作在重复取消后仍先排空后台线程，再释放算例锁，防止覆盖已确认的新编辑或重建已回收目录。实际产品双库各4项、完整并发写入SQLite93项、上传兼容1套通过。仅保证排空顺序，不声称文件系统/数据库原子提交或自动残留恢复。head4e2da9f3c865/35表、主线11完成/18进行中/24待开始（剩42）及全功能295项范围不变；正式SIWC联调按用户决定暂缓。 详见[上传取消排空](upload-cancellation-drain.md)。

N5-01不可变发布校验已移出事务，以完整本地绑定证明约束最终提交；心跳/取消在输入与包哈希期间可提交。另修复跨API终端活动复制缺口，持久会话/暂存检查与新登记使用同一算例控制锁。发布专项双库各21、终端活动各17、完整并发写入各93通过；工程5、文档5、生命周期5套兼容经原始失败保留与单套复核完成，终端执行44/暂存9项通过。可编辑来源/递归依赖/封存/别名长事务及恢复仍待完成；head4e2da9f3c865/35表、53/295与11完成/18实施中/24待实施不变。 规范证据见 `tmp/mainline-N5-publication-verify-evidence/`。

N5-01发布准备推进：原生finalizer排空后以完整发布身份/绑定快照在锁外复制、校验并刷盘产物包，提交时重验输入/包内容/来源/代数；成功提交前复核本地状态，防止准备期间心跳失败仍发布成功。真实Pandoc双库各15项、工程5/5/文档5/5/生命周期3/3通过。最终哈希/依赖/来源/别名锁及未激活目录恢复继续；head4e2da9f3c865/35表、53/295及11完成/18实施中/24待实施不变。 详见[发布包准备与提交](publication-copy-lease.md)。

N5-01收尾I/O推进：证据计算/写入、staging同步/清理、日志读取/解析转工作线程并在取消时排空；摘要按原绑定条件写回，提交后清理取消保留已提交结果。双库各29项通过，兼容8/8及隔离摘要API1/1通过。提交前任务取消仍保留待恢复finalizing，不伪造终态；发布长事务与其余N4/N5继续。head4e2da9f3c865/35表、53/295及11完成/18实施中/24待实施不变。 详见[收尾I/O与心跳](job-finalizing-io.md)。

N5-01续租推进：claim后至原生启动新增监督与核对，staging/旧输入快照转工作线程；封存输入及工作副本批量读取移到锁外，返回后重验完整绑定与发布来源。双库各18项（慢准备、Windows/WSL、五边界取消/心跳失败、输入变更）通过；兼容原8项+收尾定向8项+原生放行16+4项补齐10组。原始失败和测试等待修正均保留；head4e2da9f3c865/35表、53/295及11完成/18实施中/24待实施不变。 详见[启动续租与锁外校验](job-launch-lease.md)。

N5-01/N5-02首批：新claim持久保存真实worker出生身份，启动按case锁逐项对账；活owner（含过期租约）与未知归属不再被批量中断。精确退出owner仅在claimed/已登记running记中断审计，不重放、不伪造停止回执，starting/finalizing保留待恢复。双库专项各21、迁移各166、兼容6项原通过+终态专项双库各9通过；新增head4e2da9f3c865，仍35表，开发库未动。N5两项转实施中；53/295范围不变，主线11完成/18实施中/24待实施。 详见[Job实例身份与启动对账](job-owner-startup.md)。

N4-04：复现并修复父Job取消已提交后仍放行原生子执行；worker/发布复验最终发送与父取消同锁，准备请求复核所属记录与租约，本地关闭在最终发送前再次检查。专项双库各20（SQLite原11+定向复验9，PostgreSQL完整20）、编译/准备/Agent/runtime/SyncTeX/Job兼容8/8通过；夹具修正及原始失败日志保留。53/295、11完成/16实施中/26待实施及head3d1c98e2b754/35表不变。 详见[原生子执行放行](native-release-cancellation.md)。

N4-04：双API实际复现并修复取消已提交后仍放行命令的窗口；最终放行与取消共用case数据库锁，重验claim/worker/租约/实际登记。登记函数已返回后的启动失败保存所属停止回执；双库各8项（Windows/WSL、取消前后、通知失败、过期、放行后提交失败）及定向兼容7/7通过。53/295、11完成/16实施中/26待实施及head3d1c98e2b754/35表不变。 详见[取消与启动门顺序](job-release-cancellation.md)。

N4-03.g2b2：宿主Codex原生回合/SSE、幂等消息、复杂审批/计划问答、显式线程续接及API/Goal/审批共用执行归属已接通。双库各34回合、8暂存、6双API及115迁移约束通过；旧CLI会话各34、Agent shell各32、core14/14、协议辅助7/7通过，真实CLI本地Responses fixture6项通过，无远端推理或个人登录。head f97e54a8d310、30业务表；开发库旧13表未迁移/重载。下一单元g2b3账户登录/登出、sandbox状态/设置、WSL原生CLI与完整UI，随后专业入口、N4/N5；53/295和11完成/16实施中/26待实施不变。详见 [owned-cli-turns.md](owned-cli-turns.md)。

N4-03.g2b1：宿主CLI持久会话和正式API已接通；两级实际登记后放行，私有账户/临时目录、账户/模型展示、唯一线程创建、case/S3收尾与历史保护通过双库各34项、S3可控各9、迁移各87约束，原Agent shell各32及core14/14通过。真实Codex经正式路由读取空账户/8模型、创建线程并核验关闭，无模型推理。head e86d43f7c209、26业务表；开发库旧13表未迁移/重载。下一单元g2b2持久回合/SSE/复杂审批，再登录/WSL/UI及专业入口、N4/N5；53/295与11完成/16实施中/26待实施不变。详见 [owned-cli-sessions.md](owned-cli-sessions.md)。

API Agent shell增量：ToolCall唯一NativeRequest，原子审批、实际根/来源/权限复验及停止核验通过双库各32项，S3可控各9项。head d75c32e6b1f8、25业务表；g2实际Codex/CLI及完整N4-03继续，详见 [owned-agent-shell.md](owned-agent-shell.md)。

viz增量已验收：NativeVizSession、放行前根登记、实际kernel首帧、整树退出/缺回执、持久历史和copy/trash保护通过双库各33项；S3可控各9项，本机双API端口竞争通过。head c64b21d5a0e7、25业务表，完整N4-03继续Agent/CLI及专业入口。详见 [owned-viz-sessions.md](owned-viz-sessions.md)。

PTY后续增量：NativeSession按用户/可选case保存根与实际gate出生身份、租约及停止回执；真实重连、退出/缺回执、2MiB输入与脱离后代清理通过双库各44项，S3可控各9项。代码head b53a10c4f9d6、24业务表；详见 [owned-pty-sessions.md](owned-pty-sessions.md)。

后续SyncTeX只读请求也已接入锁外NativeRequest，历史映射/旧版来源重验、HTTP断线与实际退出、viewer权限复核通过双库各25项；继续其余探测和会话入口，详见 [owned-synctex-requests.md](owned-synctex-requests.md)。

后续正式整合：报告/LaTeX提交及工程报告原生准备已移出case写事务；HTTP断线、幂等和来源重验通过本批隔离验证，详见 [document-admission.md](document-admission.md)。原有正式作业、独立内容验证与发布协议继续适用。

准备请求新增独立归属服务，不以无效Job承载工具探测。native_requests记录身份/租约/退出证据；正式提交API现已完成本批锁外准备整合，现有Job幂等/终态协议继续适用，详见 [native-request-scopes.md](native-request-scopes.md)。代码迁移head现8b20d7f1c6a3。

N4-04新增验收：执行器、两API和SSE采用已提交逻辑退出码/结束时间；原始OS码另存。成功发布另有持久兼容收尾pending/settled，查询/重放/流等待产物写回或明确失败；远端SSE不会因无本机进程或取消已接受提前done。终态双库各9项、Pandoc各19项、LaTeX各15项、发布竞争各10项、core14/execution3/收尾通过。主线仍11完成/16实施中/26待实施，53/295范围不变；N4-03准备请求及N4-04完整竞态/N5恢复继续，见 [job-terminal-projection.md](job-terminal-projection.md)。

历史记录（Pandoc所属子执行）：报告worker/API独立复编译的Pandoc解析、HTML/DOCX编译及版本探测已接入所属原生子执行，保留stdin与独立输出/错误流；断线后等待清理再提交父终态。专项双库各16项、LaTeX各15项、发布竞争各10项、登记各20项、树各15项、生命周期12项通过；普通/工程报告各23项、core14/execution3/收尾通过。修复Windows已确认成员在读取出生时间前退出的观察竞态，保留原句柄核验与成员累计对账。主线仍11完成/16实施中/26待实施，53/295范围不变。下一单元是锁外准备请求归属和SyncTeX等入口，见 [owned-pandoc-children.md](owned-pandoc-children.md)。

历史记录（LaTeX所属子执行）：注册LaTeX worker工具探测/编译和API发布前独立复编译均由API持有原生执行范围，身份持久登记后放行，停止核验或回执提交失败阻止发布。子执行双库各15项、迁移各27项约束、发布竞争各10项、core14/14、execution3/3、收尾与LaTeX2/2（LaTeX32项）通过；新API可读取原台账。主线仍11完成/16实施中/26待实施，53/295范围不变。下一单元为其余准备探测/Pandoc/SyncTeX与会话入口，详见 [owned-native-children.md](owned-native-children.md)。

历史记录（事务外验证与持久取消）：独立产物复算/复编译已移出case写事务，最终提交重验所属快照、输入/输出哈希与发布代次；取消先持久提交再唤醒，支持另一API与finalizing。双库专项各10项、进程树各15项、core14/14、execution3/3、收尾与文档/几何4/4和网格/后处理/求解3/3通过。主线11完成/16实施中/26待实施，完整53/295范围不变。编译子执行的持久身份和实际退出等待继续N4-03，详见 [publication-preflight.md](publication-preflight.md)。

历史记录（N4-03 MPI首批）：修复私有namespace内runuser切换账户失败，WSL预先选择nobody并保留非零UID；真实ranks取消、超时、日志上限、错误身份/发行版和API猝死双库各6项通过，串并行差异1.14e-08。主线现为11完成/15实施中/27待实施，53项主线与295项功能范围不变。内部编译、API复编译、PTY/viz与Agent等剩余接线详见 [native-entry-supervision.md](native-entry-supervision.md)，N4-03保持实施中。

历史记录（N4-02最终验收）：Windows/WSL温和停止、限时升级、所属进程树退出核验和持久结果已接通；Windows增加从启动前到收尾的成员观察与累计数对账，覆盖宽限前已消失的短命成员。专项11项、双库树各15项、登记各20项及core14/14、execution3/3、契约7/7、处理器4/4、注册求解1/1通过。主线现为11完成/14实施中/28待实施，295项功能范围不变。下一单元N4-03补内部WSL/最终复编译/MPI/PTY/viz接线，详见 [process-tree-stop.md](process-tree-stop.md)。

历史记录（N4-02补充16项）：停止后派生子进程、慢清理、独立控制台/会话、调用者取消、控制EOF/非法消息、成员查询失败、WSL监督器猝死，以及真实Windows Pandoc和WSL TeX编译中途停止。执行代码与首批基线一致，前批双库与兼容结果继续适用；本批只新增测试和文档，未重复运行全集。主线仍10完成/15实施中/28待实施，N4-02继续补高频成员退出窗口与其余失败收尾，见 [process-tree-stop.md](process-tree-stop.md)。

历史记录（N4-02首批）：通用Job并行观察根退出与日志，Windows/WSL采用温和停止、限时升级及所属范围退出核验；清理结果随终态持久化并由API读取。原生进程树10项、API双库各13项、登记双库各20项通过，兼容core14/14、execution3/3、契约5/5、处理器3/3通过。主线现为10完成/15实施中/28待实施，295项功能范围不变。N4-02保持实施中，剩余fork/故障/真实编译器验收见 [process-tree-stop.md](process-tree-stop.md)。

历史记录（N4-01验收）：持久OS出生身份、Windows实际句柄与WSL双启动门、登记后放行及只读身份查询已接入通用执行器。登记双库各20项、认领双库各13项、启动门8项、Windows/WSL身份各5项通过；兼容core14/14、execution3/3、契约5/5、注册处理器5/5及网格23项通过。主线现为10完成/14实施中/29待实施，295项功能范围不变。下一单元N4-02进程树停止，详见 [process-identities.md](process-identities.md)。

历史记录（N3-06第四批）：普通HTML/DOCX与独立compose已统一注册发布，移除旧内存latest/直接写稿分支；保留旧证据门禁、覆盖和原字节备份，补齐固定生成时间、两代幂等键、历史和跨API验收。普通文档双库各23项、工程report各23/compose各25、core14/契约4/最终兼容3通过，见 [ordinary-document-workflows.md](ordinary-document-workflows.md)。主线仍9完成/14实施中/30待实施，295项功能范围不变；下一单元N4-01进程身份，N3-06完整取消/恢复前置尚未关闭。

N3-06第三批增量：全部LaTeX入口已接通注册编译、动态资源封存、按主文件持久版本、PDF/SyncTeX/BBL内容验证与历史源码定位，双库各32项通过，见 [latex-workflows.md](latex-workflows.md)。HTML/DOCX各23项、compose各25项复验；主线仍9完成/14实施中/30待实施，295项功能范围不变。这一历史增量后的普通文档统一已由第四批完成；完整取消/恢复前置仍在N4/N5推进。

N3-06第二批增量：独立报告草稿已接入注册双通道、原字节备份、覆盖保护、持久历史与HTML/DOCX生成来源，双库各25项通过，见 [report-compositions.md](report-compositions.md)。HTML/DOCX双库各23项复验；主线仍为9完成/14实施中/30待实施，295项功能范围不变。接下来继续LaTeX注册，N3-06整项未关闭。

2026-11-02增量：本地取消接口支持queued且未claim的Job；与claim共用case控制锁，复查最近attempt后提交cancelled/outbox。已claimed返回状态变化提示，运行中的跨worker取消仍需N4。报告构建启动冲突409，保留既有终态；证据见 [case-write-coordination.md](case-write-coordination.md)。

2026-11-02输入版本增量：显式`input_paths`在case锁内封存输入并创建独立run；执行器及内部queue_step已接通，受限shell重试从旧封存版本开始；详情附workspace/outputs。双库各23项、writes26和兼容5套通过，见 [attempt-workspaces.md](attempt-workspaces.md)。

2026-11-02几何发布增量：默认preview/export及自动预览已接通封存输入、专属run和持久publication；Job终态/head/事件同事务提交，兼容文件写失败不撤销成功。双库各16项及core14/writes30/CAE3通过，见 [geometry-publication.md](geometry-publication.md)。

2026-11-02发布输入增量：Job/queue_step可选择input_publications，封存指定产物且保存来源；默认current策略在启动和最终事务复核，历史请求必须显式选择；重试保留原输入与策略。双库各19项及几何16/attempt23/compatibility4/execution3通过，见 [publication-inputs.md](publication-inputs.md)。

日期：2026-11-02。N1-04、N1-05、N1-06仍为实施中；本页记录已实现和实际验收的部分，不代表全部工作流、恢复或分布式能力完成。执行状态见 [主线台账](mainline-task-status.json)，总体范围仍为53项主线和295项功能。

## 请求与领取

普通 `POST /api/v1/cases/{id}/jobs` 支持可选 `Idempotency-Key`，限制为1–128个可见ASCII字符。作用域为case+发起用户；先检查case权限，再查询请求。相同键及规范化请求体返回原Job，不因进程结束或API重启创建新Job；不同请求体返回409/idempotency_conflict。无键的请求保持独立提交。已提交请求的回放先于当前文件/配额检查，新请求仍须通过原检查。

`job_execution.submit` 在同一事务保存Job、queued attempt、编号head、请求哈希、执行规格、事件与outbox。执行规格包括命令、runtime、case目录、argv、预算和证据run_dir；它是启动参数记录；显式提供input_paths时另存workspace封存清单，未提供时仍没有不可变工程输入副本。事件不包含命令/claim凭证。批量及专用handler尚未全部支持HTTP幂等键；它们通过共同执行器获得启动领取保护。

`start_job` 在staging、内存注册和OS spawn之前提交原子claim。case控制写入统一锁顺序，随后验证Job仍queued、活动尝试指针、版本和取消状态，安装随机claim token、worker、心跳和租约。重复worker收到`JobAlreadyClaimed`，不会启动子进程或覆盖另一个worker的注册记录。worker回调验证attempt、root Job、token和活动head；相同终态的无操作也不能绕过旧尝试检查。Redis适配器收到重复领取拒绝可返回，但本轮双worker验收使用独立真实进程和数据库，没有启动真实Redis运输层。

已有Job命令必须与交付命令一致；普通提交还要求完整执行规格一致，拒绝队列参数替换。专用handler的argv在首次claim记录，后续完整handler签名/版本管理随工作流调度补齐。

## 持久阶段、结束与重试

真实执行器按 `queued → claimed → starting → running → finalizing → terminal` 更新JobAttempt。starting提交先于spawn；running与Job.running同事务；原生退出之后先提交finalizing，再运行专业校验、报告hook、同步和终态提交。最终Job、attempt及事件一起提交后才完成wait/SSE通知。资源上限、证据、同步、发布和启动失败分别记录原因码。

详情 `GET /jobs/{id}` 新增attempt摘要：id、number、root_job_id、phase、reason_code、retry_of_attempt_id。不返回worker token。无本机proc的历史详情也能查询phase。兼容列表的旧phase仍依赖本机proc，完整列表聚合由N1-07/N6完成。SQLite读取的无时区ended_at按UTC解释，修复重启后历史时间偏移。

默认每10秒更新心跳/30秒观察租约，可由 `VIBEFLOW_JOB_HEARTBEAT_SECONDS` / `VIBEFLOW_JOB_LEASE_SECONDS` 配置；心跳发现持久取消会请求本次owned scope退出。**租约到期不自动重领或重放**。新claim另存实际worker出生身份，见[job-owner-startup.md](job-owner-startup.md)。原生根记录已绑定真实宿主/WSL出生身份、attempt和worker；running事务提交后才放行用户命令，详见process-identities.md。温和停止及通用范围退出已由N4-02验收；旁路后代与完整恢复继续N4-03/N5。旧Job API的cancelled兼容语义为取消已接受，attempt.cancelling和finished表示是否仍在排空，不能把取消响应当作进程树已清理。

重试编号由根Job的head在事务锁下递增，不使用裸max()+1。内部 `allocate_retry` 为受限普通shell创建新Job和新attempt，关联旧attempt；旧Job、日志路径、结束时间和输入规格记录保留。新的活动head使旧回调失效。该函数只分配，不派发，没有对外重试端点；专业handler的冻结输入、retry/rerun/resume区分、审批工具和远端资源策略仍由N2/N5/N6完成，不自动重放未知命令。

## 迁移与证据

新增固定迁移 `4c02b1a7d9e0`，前置 `0fd328dae4cf`：新增job_submission_requests，给attempt补root_job_id/retry_of_attempt_id及请求族编号唯一约束。旧attempt的root回填为job_id。代码共22业务表，旧12表字段不改。

| 验证 | 结果与边界 | 项目根目录证据 |
|---|---|---|
| 双数据库增量迁移 | SQLite/PostgreSQL各26项约束；带旧attempt/head/event/publication的上一版本往返；外键完整、metadata diff=0；旧Job保留 | tmp/workflow-migration-hwrm5_mj/result.json |
| 双数据库领取与重试 | 各13项；8个并发提交一Job；两个真实worker只启动一个原生进程；finalizing屏障；重试旧日志保留；伪造/迟到所有者拒绝；queued/claimed取消；claim提交失败零spawn | tmp/job-claim-zguxoh0f/result.json |
| HTTP与重启 | 6个并发HTTP同键一Job/一attempt；409冲突、非法键、权限先行、跨case/用户隔离；真实API重启同Job/日志/阶段 | tmp/job-idempotency-api-a65n70st/idempotency-evidence.json |
| 最终回归 | offline18、core14、execution3、真实CAE3；Python入口176个，未执行全集 | tmp/mainline-N1-claim-evidence/{verified-offline,release-core,verified-execution,verified-cae}/summary.json |

首轮沙箱内WSL返回E_ACCESSDENIED，造成依赖OpenFOAM/LaTeX的测试失败；最终core/CAE使用沙箱外的独立测试目录和数据库复验，没有放宽数值断言。迁移测试只创建并删除随机命名临时PostgreSQL库。开发库仍为旧12业务表/基线版本，未迁移、未重载旧开发API。源码版本单独归档，不包含用户算例、凭据或运行数据。

本里程碑源码归档：`platform/backups/mainline-N1-claim-20261102.zip`；整包SHA-256见同名`.zip.sha256`，包内含逐文件哈希清单。恢复必须使用新目录，不覆盖用户数据。

## 后续实施

随后N1-03的依赖/失败传播/确认核心已通过双数据库验收，见 [workflow-scheduler.md](workflow-scheduler.md)；N2-01写入协调已验收，下一单元为N2-02剩余入口，见 [case-write-coordination.md](case-write-coordination.md)。N1-04保留专用/工作流入口幂等接线和符合输入证据约束的重试验收；N1-05保留报告预处理失败入口、跨重启finalizing恢复和真实队列确认；N1-06保留业务副作用消费者去重。提交已入DB但尚未入队/进程尚未启动的崩溃窗口还需要调度outbox或恢复对账，不能宣称任意命令恰好执行一次。

启动现按持久worker出生身份逐项对账；活实例、未知归属和未领取投递不再被批量中断。精确owner退出的starting/finalizing保留待恢复，完整队列重投、登记窗口、发布补交和多主机恢复仍未完成。显式输入目录已接通；其余专业入口迁移及发布head、文件与DB提交日志、完整取消树和工作流UI继续。V01/V08仅有本页列出的子场景证据，V18仍只有outbox补投部分，不关闭任何整个故障场景或FF功能。
