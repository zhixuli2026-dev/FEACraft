# 执行、取消、重试和产物入口审查（N0-02）

2026-11-07：业务原生助手先持久登记身份再同进程交接，v4补齐新请求host_ready恢复证据；361个源码、256个路由、373个静态边界，数量不代表覆盖率。见[双库实际中断与真实模型证据](native-preflight-handoff.md)。

2026-11-07：业务清理已接入独立 Windows Job 守护、v3 许可/关闭及未知宿主出生前恢复；360个源码、256个路由、373个静态边界，数量不代表验收覆盖率。见[实际双库退出与故障证据](windows-guardian-business-recovery.md)。

2026-11-07：业务宿主创建前增加持久admitted边界，v2未许可与历史未知身份明确区分；357个源码、256个路由、370个静态边界。实际双库中断和阻断证据见[启动许可恢复](native-scope-admission-recovery.md)，数量不代表覆盖率。

2026-11-07：新增业务作用域持久生命周期与执行器资源收尾钩子；357个源码、256个路由、370个静态边界。统计不是覆盖证明，实际分配/入场/撤销和双库证据见[业务生命周期](native-scope-business-lifecycle.md)。

2026-11-07：新增实际创建前许可握手与空分配恢复；356个源码、256个路由、370个静态边界。统计仅为源码入口清单，实际证据见[分配与所有者退出验收](native-scope-allocation.md)。请求数据库接入仍待。

2026-11-07：新增独立的固定原生作用域控制通道，创建/观察/撤销均需实际控制者退出；不扩展只读身份查询权限。启动意图持久绑定宿主及控制实现，实际descriptor的请求连接仍待。356个源码、256个路由、367个静态边界；[实际控制与双库证据](native-scope-control.md)补充静态统计，不能由边界数量推断验收覆盖。

2026-11-07：Windows宿主创建统一接入原子Job列表与显式stdio句柄继承，保留实际创建句柄和完整退出观察；354个源码、256个路由、366个静态边界。新增原生ctypes创建点不由当前AST分类单独计数，须结合[实际创建/所有者退出验证](windows-job-creation.md)阅读；统计不是运行覆盖证明。cgroup启动门能力已有实际证据，持久恢复连接仍待。

2026-11-07：原镜像清理区分活动请求所有者退出与已终止请求隔离；352个源码、256个路由、366个边界。实际子进程退出要求不变，准备取消的节点预留经实际核验后只释放当前调用持有项。见[专项证据](sandbox-image-live-owner.md)。

2026-11-07：清理请求接入有界持久启动观察；352个源码、256个路由、366个边界。created提交后才允许进入host_ready，host_ready提交后才放行WSL启动；没有完整已知出生集合仍拒绝恢复。见[启动证据与实际验收](sandbox-image-startup.md)。

2026-11-07：原镜像清理增加独立退出对账；351个源码、256个路由、366个边界。GET只查询，POST明确重试时核实旧所有者和登记子进程退出、提交完整绑定后才启动新清理；无登记身份仍拒绝。见[专项证据与剩余窗口](sandbox-image-reconciliation.md)。

2026-11-07：原镜像清理增加2个API入口、terminal.cleanup登记purpose与固定外层gate清理。当前350个源码、256个路由、366个边界。镜像删除始于原生登记/释放后，核验退出与卷身份，不重放命令；实际验收和猝死对账缺口见[原镜像清理](sandbox-image-cleanup.md)。

2026-11-07：原始暂存清理新增GET状态与POST确认2个入口；当前348个源码、254个路由、366个边界。写入口在持久意图提交后、算例控制锁内排空逐项清理，完成事件前阻止管理操作，无原生推理启动。见[原始暂存清理及剩余矩阵](sandbox-staging-cleanup.md)。

2026-11-07：选中文件内容恢复新增预览、显式确认写回和回执查询3个入口。机器清单为346个源码、250个路由、365个边界。写入口复用容量准入、算例写锁、FS/S3文件事务及持久事件；不启动原生推理或重放终端命令。实际双后端和浏览器证据及尚未覆盖的专属故障矩阵见[文件内容写回](sandbox-recovery-restore.md)。

2026-11-06：机器清单已刷新并校验，当前337个源码文件、242个路由、360个边界记录（含21个原生进程调用点）。新增提交事务 helper 的调用边界见 [提交额度](submission-capacity.md)。此盘点不等于所有运行时清理已通过，N4-03当前核对与剩余工作见 [当前入口核对](N4-03-current-audit.md)。下方分批迁移记录保留历史语境，整项状态以主线台账为准。

2026-11-05：当前18个提交调用点、25个注册处理器核对完毕；所有公开新请求封存输入，旧无封存投递被拒绝。原legacy Admission.submit已无产品调用者，仍使用其准备/响应辅助函数。详见[输入封存检查](unsealed-job-delivery.md)；下文旧迁移边界为历史记录。

S3 普通 shell/批次、五种通用类型命令与显式文件选择已接入独立尝试；通用类型命令发布不可变结果，通过远端条件操作计划更新兼容文件，并支持独立缓存的结果引用。正式专业处理器和完整递归工作流继续，详见[S3 类型命令发布](s3-typed-publication.md)。

2026-11-05：本地 `/jobs` 的普通 shell 默认封存；五种显式类型命令默认独立执行并发布，`run_dir` 保留真实求解证据。提交绑定不改变调用方的投递策略。旧内部绝对路径 argv 与 S3 继续迁移，详见[类型命令发布](typed-command-publication.md)。

N3-01按原统一接口与拒绝shell伪造证据条件关闭：九处理器共用契约，当前真实CadQuery双库各15项通过，并核对原专业验收与新发布恢复。N3-02后续仍受原N2前置约束，N4/N5未交付内容及295项范围全部保留。主线19完成/14进行中/20待开始，剩34；295项严格完整交付仍0。 详见[原条件收口核对](mainline-acceptance-review.md)。








N5-02逐项对账结论已持久化：旧/未知owner、未投递、缺登记和待发布收尾保存绑定完整状态的审计及outbox，Job详情显示当前原因；锁外身份观察后复验，取消/续租不阻塞，重复结论去重，暂时核验失败解除后保留审计。专项双库各17、真实Windows/WSL所有者兼容各21及真实API重启通过。安全投递意图、批次/工作流约束、终态缺证与finalizing实际补交继续；主线11完成/19进行中/23待开始，剩42，全功能295严格验收仍0。 详见[作业对账结论](job-reconciliation-decisions.md)。

N5-03独立请求和会话启动窗口已补齐验收：Windows/WSL准备请求、cmd/WSL/sandbox终端、Windows可视化、Windows/非root WSL CLI共8配置，真实创建/握手后API退出与登记提交确认丢失，SQLite/PostgreSQL各24项通过。原有产品实现满足本批条件；未知停止保留需处理状态，原持有者实际清理才保存停止证明。N5-03仍受N5-02前置整体验收约束；主线11完成/19进行中/23待开始，剩42，全功能295项严格验收仍0。正式SIWC联调继续暂缓。 详见[会话启动登记恢复](session-startup-recovery.md)。

N5-03根作业部分握手与Job子执行登记证据已补齐：实际创建/宿主/WSL握手分别留痕，失败登记保存原持有者实际清理，完整失败子执行历史可恢复排空检查点；未知记录只审计、不重放。Windows子执行双库各22项、WSL子执行各25项、根握手各15项、排空兼容各24项及真实Pandoc1/LaTeX2通过。旧Pandoc收尾回执故障在上一验收归档同样复现，留待N5-04；其余request/会话覆盖仍待核对。主线11完成/19进行中/23待开始，剩42；全功能295项严格验收仍0，正式SIWC联调暂缓。 详见[启动握手与子执行登记恢复](child-launch-registration.md)。

N5-03根作业启动登记窗口已补齐首批：创建进程前持久意图，准备身份先于running，登记确认丢失仍保留实际清理；重启在锁外核验并持久记录需处理状态，阻止重复认领。最终Windows/WSL双库各37项、既有5套兼容通过；更早31+5项及夹具失败分别保留。子执行/握手内部窗口、finalizing和文件恢复继续。主线11完成/19进行中/23待开始（剩42），全功能295项严格验收仍为0；head6a4fc1b5e087/36表未变，正式SIWC联调暂缓。 详见[启动登记与重启核对](job-launch-registration.md)。

N5-01/N5-02已补齐有完整停止证据的历史Job写入排空恢复：短事务读取、锁外核验原实例已退出、再锁定复验并原子保存v2证明和审计。双库各25项、主目录SQLite含真实WSL复核25项及占用32/启动21/作业写入25项兼容通过。登记不完整、缺物理证据、收尾中断、文件恢复及其余长事务仍待完成。主线11完成/18进行中/24待开始（剩42）；295项全功能严格验收仍为0。head6a4fc1b5e087/36表不变，正式SIWC联调仍暂缓。 详见[历史作业排空恢复](job-drain-recovery.md)。

N5-01实际发布已接入占用下的来源/递归依赖锁外校验：写者及兼容副本先排空，完整数据库观察和本地证明在最终事务复验，成功释放前再查期限；新批量提交保存未claim身份。双库各19项（完整来源15+排空3，另定向复验提交前过期1）、复制15/不可变校验21、真实组合报告/HTML-DOCX/LaTeX兼容及访问16/占用32/Job25/批量1套通过；原始失败、夹具顺序修正和探索结果分别保留。早期依赖读取、确认/GET/封存/副本长事务及旧记录恢复继续。head6a4fc1b5e087/36表，开发库和端口8000未动；主线11完成/18进行中/24待开始（剩42），全功能295项严格交付0，正式SIWC联调仍暂缓。 详见[发布来源与依赖校验](publication-source-validation.md)。

N5-01封存与读取边界已补齐：seal在复制前拒绝占用，提交/重试冲突不留文件或记录；21个读取入口及批量取消保留可用，原权限/损坏检查继续生效。双库各16项、封存SQLite23项、占用32项及真实批量作业回归通过；WSL受限环境失败和授权复验分别保留。实际发布来源/依赖锁外校验、旧记录及封存/别名恢复仍待完成。head6a4fc1b5e087/36表，开发库和端口8000未动；主线11完成/18进行中/24待开始（剩42），全功能295项范围不变。 详见[校验期间的内容与读取](validation-content-access.md)。

N5-01作业写入互斥已接通：claim/原生与子执行放行检查占用，取得占用前要求完整排空检查点；真实Job占用后写入窗口已关闭，已排空并行收尾可依次校验，重复发布准备被拒绝。双库各25项、迁移各166项、占用SQLite32项、兼容4/4及最终Pandoc2项通过。旧Job缺证恢复、其余直接写入及真实发布来源/依赖锁外校验继续；head6a4fc1b5e087/36表，开发库和端口8000未动。主线11完成/18进行中/24待开始（剩42），全功能295项范围不变。 详见[Job写入排空与校验准入](validation-job-admission.md)。

N5-01原生会话互斥已接通：终端/CLI/viz/request登记与校验占用共用case控制锁，活动、过期、未知停止及暂存残留阻止校验；真实终端复现窗口已关闭。双库各35项、Agent32/CLI36/PTY44/可视化33/占用32项兼容完成；原始4/5与可视化单套复核1/1分别保留。Job写入、其余直接入口和真实发布锁外校验尚未接通。head5f3eb0a4d976/36表，开发库和端口8000未动；主线11完成/18进行中/24待开始（剩42），全功能295项范围不变。 详见[原生会话与校验准入](validation-native-admission.md)。

N5-01来源校验基础协议已实现：持久占用、跨实体外键、精确令牌/代数检查、独立提交过期撤销及协调写入拒绝；四个文件只读入口保持可用。双库各32项、迁移各166项、现有兼容4/4（完整写入SQLite93项）通过。正式发布尚未创建占用，已知原生写者准入和真实来源/依赖锁外校验继续；不能据此宣称长事务已解决。新head5f3eb0a4d976/36表，开发库及端口8000未操作；主线11完成/18进行中/24待开始（剩42），全功能295项范围不变。 详见[来源校验占用](case-validation-reservation.md)。

上传取消的线程越界已修复：创建/写块/完成/取消四个操作在重复取消后仍先排空后台线程，再释放算例锁，防止覆盖已确认的新编辑或重建已回收目录。实际产品双库各4项、完整并发写入SQLite93项、上传兼容1套通过。仅保证排空顺序，不声称文件系统/数据库原子提交或自动残留恢复。head4e2da9f3c865/35表、主线11完成/18进行中/24待开始（剩42）及全功能295项范围不变；正式SIWC联调按用户决定暂缓。 详见[上传取消排空](upload-cancellation-drain.md)。

N5-01不可变发布校验已移出事务，以完整本地绑定证明约束最终提交；心跳/取消在输入与包哈希期间可提交。另修复跨API终端活动复制缺口，持久会话/暂存检查与新登记使用同一算例控制锁。发布专项双库各21、终端活动各17、完整并发写入各93通过；工程5、文档5、生命周期5套兼容经原始失败保留与单套复核完成，终端执行44/暂存9项通过。可编辑来源/递归依赖/封存/别名长事务及恢复仍待完成；head4e2da9f3c865/35表、53/295与11完成/18实施中/24待实施不变。 规范证据见 `tmp/mainline-N5-publication-verify-evidence/`。

N5-01发布准备推进：原生finalizer排空后以完整发布身份/绑定快照在锁外复制、校验并刷盘产物包，提交时重验输入/包内容/来源/代数；成功提交前复核本地状态，防止准备期间心跳失败仍发布成功。真实Pandoc双库各15项、工程5/5/文档5/5/生命周期3/3通过。最终哈希/依赖/来源/别名锁及未激活目录恢复继续；head4e2da9f3c865/35表、53/295及11完成/18实施中/24待实施不变。 详见[发布包准备与提交](publication-copy-lease.md)。

N5-01收尾I/O推进：证据计算/写入、staging同步/清理、日志读取/解析转工作线程并在取消时排空；摘要按原绑定条件写回，提交后清理取消保留已提交结果。双库各29项通过，兼容8/8及隔离摘要API1/1通过。提交前任务取消仍保留待恢复finalizing，不伪造终态；发布长事务与其余N4/N5继续。head4e2da9f3c865/35表、53/295及11完成/18实施中/24待实施不变。 详见[收尾I/O与心跳](job-finalizing-io.md)。

N5-01续租推进：claim后至原生启动新增监督与核对，staging/旧输入快照转工作线程；封存输入及工作副本批量读取移到锁外，返回后重验完整绑定与发布来源。双库各18项（慢准备、Windows/WSL、五边界取消/心跳失败、输入变更）通过；兼容原8项+收尾定向8项+原生放行16+4项补齐10组。原始失败和测试等待修正均保留；head4e2da9f3c865/35表、53/295及11完成/18实施中/24待实施不变。 详见[启动续租与锁外校验](job-launch-lease.md)。

N5-01/N5-02首批：新claim持久保存真实worker出生身份，启动按case锁逐项对账；活owner（含过期租约）与未知归属不再被批量中断。精确退出owner仅在claimed/已登记running记中断审计，不重放、不伪造停止回执，starting/finalizing保留待恢复。双库专项各21、迁移各166、兼容6项原通过+终态专项双库各9通过；新增head4e2da9f3c865，仍35表，开发库未动。N5两项转实施中；53/295范围不变，主线11完成/18实施中/24待实施。 详见[Job实例身份与启动对账](job-owner-startup.md)。

N4-04：复现并修复父Job取消已提交后仍放行原生子执行；worker/发布复验最终发送与父取消同锁，准备请求复核所属记录与租约，本地关闭在最终发送前再次检查。专项双库各20（SQLite原11+定向复验9，PostgreSQL完整20）、编译/准备/Agent/runtime/SyncTeX/Job兼容8/8通过；夹具修正及原始失败日志保留。53/295、11完成/16实施中/26待实施及head3d1c98e2b754/35表不变。 详见[原生子执行放行](native-release-cancellation.md)。

N4-04：双API实际复现并修复取消已提交后仍放行命令的窗口；最终放行与取消共用case数据库锁，重验claim/worker/租约/实际登记。登记函数已返回后的启动失败保存所属停止回执；双库各8项（Windows/WSL、取消前后、通知失败、过期、放行后提交失败）及定向兼容7/7通过。53/295、11完成/16实施中/26待实施及head3d1c98e2b754/35表不变。 详见[取消与启动门顺序](job-release-cancellation.md)。

N4-03.h：核对236个app源码的旧worker/条件回退；退役无正式调用者的report_worker，Pandoc/LaTeX必须具有所属通道，CLI发现不再使用默认WSL runner。通道14、发现7、LaTeX双库各15、Pandoc双库各19、原生准备14、文档准备16、普通文档23、快照10通过；首轮误删导入失败及修正重跑分别保留。53/295、11完成/16实施中/26待实施及head3d1c98e2b754/35表不变。 详见[原生编译所属通道](required-native-channels.md)。

N4-03.h：复现并修复API退出后WSL只读身份探测残留；身份/ancestry改用所属Windows范围、固定Linux读程序、10秒原生期限及管道退出。专项11（取消0.328秒/API猝死0.671秒/期限10.532秒）、登记双库各20、PTY双库各44、CLI/基础兼容5/5通过（实际Linux CLI10、准备9、传输7）。不创建业务Job，不改变原持久登记后放行；53/295、11完成/16实施中/26待实施和head3d1c98e2b754/35表不变。 详见[所属只读身份探测](owned-control-readers.md)。

N4-03.h继续：真实CalculiX增量计算三种中断3项、FloTHERM实际编译4项、PPT/PDF/FloTHERM转换实际处理检查点中断9项、共享夹具UDF兼容4项通过。worker检查点为明确测试暂停；部分转换输出可保留，不代表原子发布或N5恢复。仅测试/文档改动；53/295、11完成/16实施中/26待实施、head3d1c98e2b754/35表不变。 详见[专业进程故障后续批次](professional-process-faults.md)。

N4-03.h第二批：专业启动冲突现在保留已提交Job ID；九入口登记拒绝及无Job/跨用户边界共11项、真实UDF编译器4项、ParaView/CadQuery实际库与脚本后代6项、兼容6/6及SQLite/PostgreSQL登记各20项通过。owner猝死仅证明实际退出；跨运行时后代、其余专业停止和N5恢复继续保留。head仍3d1c98e2b754/35业务表，开发库未动；53/295及11完成/16实施中/26待实施不变。 详见[专业进程故障](professional-process-faults.md)。

N4-03.h后续：WSL私有目录初始化已从直接subprocess移入登记后的Linux bridge；准备后重新核验配置、访问权和执行归属，再单独放行CLI。初始化专项9、双库会话各16、实际Linux CLI10、目录13/传输7/文件sandbox、宿主兼容5/5及profile历史/拒绝7项通过。owner变更仅清理自己持有的进程，不伪造持久回执；残留目录/未知结果继续N5。head仍3d1c98e2b754/35业务表，开发库未动，53/295和11完成/16实施中/26待实施不变。 详见[所属CLI初始化](owned-cli-state-preparation.md)。

N4-03.h首批：CalculiX/SU2改为固定argv，修复Windows shell展开合法文件名的故障；实际求解4项、SU2取消/超时/owner猝死3项、专业兼容8/8通过。复核13个Job/3个原生子执行的停止回执。另补齐新CLI启动入口WSL实际10项，6个启动请求均关联原生退出。继续准备helper监督、专业/几何库故障矩阵及其余N4/N5；head仍3d1c98e2b754/35业务表，开发库未动，53/295和11完成/16实施中/26待实施不变。 详见[专业入口监督](professional-entry-supervision.md)。

N4-03.g2b3i：高层CLI会话启动已加入独立幂等记录、会话执行归属、后台启动、只读查询与取消；浏览器刷新/响应丢失不自动发消息，显式重试复用原key，消息拒绝不误关闭其他标签页已使用的CLI。双库启动各16、完整迁移各164/35表、兼容6/6、Windows真实CLI3项和浏览器13项通过，独立界面复查ship，构建通过。head3d1c98e2b754，开发库未动；N5未知启动对账及完整WSL/其他N4继续，53/295和11完成/16实施中/26待实施不变。 详见[原生启动回执](native-cli-startup.md)。

N4-03.g2b3h：原生推理强度/摘要/本次服务档位已接入，计划模式和容量续跑保留显式参数，参数进入幂等摘要与历史回执；CLI模型目录分页、嵌套字段过滤和前台候选项已接通。双库参数各11/回合各40、兼容7/7、Windows真实CLI3项及浏览器10项通过，独立界面复查ship，构建通过。请求参数不等于实际收费档位，完整模型/WSL矩阵和其余N4/N5继续；head仍2c0b87d1a643/34表，开发库未动，53/295与11完成/16实施中/26待实施不变。 详见[原生模型参数](native-cli-model-options.md)。

N4-03.g2b3g：持久化原生Token用量快照，绑定实际尝试，区分最近模型调用/线程累计；重复通知去重，续跑无报告不复用旧数字，未知/零/无效报告分离，支持历史读取与折叠表格。双库用量各20/原生回合各40、兼容4/4、Windows真实CLI3项与浏览器9项通过，界面独立复查ship，构建通过。此为观测快照，未实现账户额度、价格结算、全局用量聚合或预算强制；head仍2c0b87d1a643/34表，开发库未动，53/295与11完成/16实施中/26待实施不变。 详见[原生用量快照](native-cli-usage.md)。

N4-03.g2b3f：原生聊天接入图片选择/粘贴/移除/纯图发送与持久历史；最多3张、每张4MiB，CLI读取私有副本并核验字节，平台保存算例文件引用，响应丢失只查询且不重发。双库图片各17/回合各40、兼容4/4+2/2+API图片1/1、Windows真实CLI4项/WSL5项及浏览器13项通过；WSL同时验证带图原生fork与父子独立，界面独立复查ship，构建通过。head仍2c0b87d1a643/34业务表，开发库未迁移/重载；原生其他输入/完整模型/MCP/用量、正式SIWC与sandbox部署、N4/N5继续，53/295和11完成/16实施中/26待实施不变。 详见[原生图片接入](native-cli-images.md)。

N4-03.g2b3e：CLI原生fork已接通持久准入、指定完成回合边界、双会话执行权、幂等查询、第一条消息原生续接、刷新发现及取消收尾；结果不确定时保留待恢复，禁止自动重建。分叉API双库各13、原生回合各40、迁移各147约束/34业务表、兼容5/5、实际Windows CLI本地Responses5项及浏览器12项通过，前端构建通过；UI独立复查NF-01已修正并复核。head2c0b87d1a643，开发库未迁移/重载；附件/多模态/完整模型/MCP/用量、正式SIWC/WSL/sandbox部署及N4/N5继续；53/295及11完成/16实施中/26待实施不变。 详见[原生CLI分叉](native-cli-forks.md)。

N4-03.g2b3d：统一Agent面板接入API/Windows/WSL原生聊天、只读刷新重连、响应丢失消息查询、复杂审批、取消收尾及运行环境检查；新线程冻结有界文字历史，API历史变化后拒绝恢复旧原生线程。原生API双库各40、会话专项5、SSE客户端8、历史4组、兼容3/3及浏览器18项通过，前端构建通过；UI独立复查F1/F2已修正并核验。无迁移或开发服务重载；head仍1b9a76c0f532/33业务表。CLI附件/原生fork/用量等完整功能、正式SIWC/WSL/sandbox部署、N4其余入口/预算与N5恢复继续；53/295及11完成/16实施中/26待实施不变。 详见[统一原生聊天](native-chat-ui.md)。

N4-03.g2b3c：正式CLI会话支持Windows/WSL选择；WSL采用Linux原生程序、非root启动、原生私有home、两级登记/出生核验、线程续接及退出回执，新增Windows sandbox就绪查询。真实挂载目录EPERM已修复，配置撤销保留409。WSL双库各16、Windows各36、原生目录13/传输7/真实回合7、实际Linux文件sandbox及兼容7/7通过；迁移各131项。head1b9a76c0f532/33业务表；开发库只读仍13表旧head，未迁移或重载。Windows本机legacy未配置/MXC不可用；正式WSL目录、sandbox管理员设置、SIWC公网联调和聊天UI仍待。53/295及11完成/16实施中/26待实施不变。 详见[WSL CLI与sandbox说明](owned-cli-wsl.md)。

N4-03.g2b3b：托管网站 Sign in with ChatGPT 已接通持久授权事务、PKCE/nonce/签名验证、明确身份关联、取消/退出、HttpOnly Cookie 与账户 UI；API 重启/双实例认领、迟到回调、撤销和可视化凭据隔离已验证。双库登录各37、迁移各130项约束、浏览器14、core14/14、CAD/可视化2/2、前端build通过；首次core因沙箱拒绝WSL为8/14，获准访问WSL后完整通过。正式OpenAI client/公网回调仍未联调，开发库未迁移。继续sandbox、WSL原生CLI和完整聊天UI/N4/N5；53/295及11完成/16实施中/26待实施不变。 详见[网站登录实现与部署说明](website-sign-in.md)。

N4-03.g2b3a：按用户确认的托管/商业部署，原生CLI接入平台API凭据，固定provider/版本、隔离私有home、启动/审批/续租/交付复验，轮换或撤销停止旧scope；修复拒绝resume的清理互等。双库凭据各21、回合/会话各34、暂存各8、双API各6、辅助7/7通过；实际CLI经产品BYOK访问本地Responses共7项，含工具shell不继承密钥。没有远端推理/个人登录；开发库未迁移。继续网站Sign in with ChatGPT、sandbox/WSL/UI及完整N4/N5；53/295和11完成/16实施中/26待实施不变。 详见 [hosted-cli-credentials.md](hosted-cli-credentials.md) 和[部署决策](hosted-agent-auth.md)。

N4-03.g2b2：宿主Codex原生回合/SSE、幂等消息、复杂审批/计划问答、显式线程续接及API/Goal/审批共用执行归属已接通。双库各34回合、8暂存、6双API及115迁移约束通过；旧CLI会话各34、Agent shell各32、core14/14、协议辅助7/7通过，真实CLI本地Responses fixture6项通过，无远端推理或个人登录。head f97e54a8d310、30业务表；开发库旧13表未迁移/重载。下一单元g2b3账户登录/登出、sandbox状态/设置、WSL原生CLI与完整UI，随后专业入口、N4/N5；53/295和11完成/16实施中/26待实施不变。详见 [owned-cli-turns.md](owned-cli-turns.md)。

N4-03.g2b1：宿主CLI持久会话和正式API已接通；两级实际登记后放行，私有账户/临时目录、账户/模型展示、唯一线程创建、case/S3收尾与历史保护通过双库各34项、S3可控各9、迁移各87约束，原Agent shell各32及core14/14通过。真实Codex经正式路由读取空账户/8模型、创建线程并核验关闭，无模型推理。head e86d43f7c209、26业务表；开发库旧13表未迁移/重载。下一单元g2b2持久回合/SSE/复杂审批，再登录/WSL/UI及专业入口、N4/N5；53/295与11完成/16实施中/26待实施不变。详见 [owned-cli-sessions.md](owned-cli-sessions.md)。

N4-03.g2a：所属stdio和实际Codex协议基础已验收；22项进程/通信专项、7套定向回归，以及真实CLI对本地Responses fixture的完成/失败/中断/恢复/分叉通过，空账户且无远端推理。双启动回调尚未接数据库；下一单元g2b接用户/会话持久台账、正式API、账户/复杂审批、case/S3及WSL原生CLI，再继续专业入口和N4/N5。head仍d75c32e6b1f8/25业务表，开发库未迁移/重载，53/295与11完成/16实施中/26待实施不变。详见 [owned-codex-transport.md](owned-codex-transport.md)。

API Agent shell增量：ToolCall唯一NativeRequest，原子审批、实际根/来源/权限复验及停止核验通过双库各32项，S3可控各9项。head d75c32e6b1f8、25业务表；g2实际Codex/CLI及完整N4-03继续，详见 [owned-agent-shell.md](owned-agent-shell.md)。

viz增量已验收：NativeVizSession、放行前根登记、实际kernel首帧、整树退出/缺回执、持久历史和copy/trash保护通过双库各33项；S3可控各9项，本机双API端口竞争通过。head c64b21d5a0e7、25业务表，完整N4-03继续Agent/CLI及专业入口。详见 [owned-viz-sessions.md](owned-viz-sessions.md)。

PTY后续增量：NativeSession按用户/可选case保存根与实际gate出生身份、租约及停止回执；真实重连、退出/缺回执、2MiB输入与脱离后代清理通过双库各44项，S3可控各9项。代码head b53a10c4f9d6、24业务表；详见 [owned-pty-sessions.md](owned-pty-sessions.md)。

全局Python/WSL能力探测已接入按用户所属、case为空的请求范围，固定用途、实际退出与降级语义通过双库各25项；不分配Job。代码head ad42f9b3e8c5，仍23业务表，详见 [owned-runtime-probes.md](owned-runtime-probes.md)。

SyncTeX原直接启动已由请求所属范围替换，历史与旧版来源固定、只读工具环境及权限重验通过双库各25项。静态清单197源/191路由/275边界；全局runtime探测和会话入口继续，详见 [owned-synctex-requests.md](owned-synctex-requests.md)。

本批报告/LaTeX及工程报告准备已接入请求原生范围，case写事务不覆盖解析/探测；静态清单193源/191路由/271边界，详见 [document-admission.md](document-admission.md)。其余原生请求与会话入口继续N4-03。

请求范围服务已复用正式原生执行器，静态清单现192源/191路由/267边界；原生准备尚待接入正式报告/LaTeX及workflow入口，见 [native-request-scopes.md](native-request-scopes.md)。

本批Job终态字段、跨API SSE及兼容文件收尾已采用持久数据；没有本机进程不再直接发done。收尾pending/settled和源码证据见 [job-terminal-projection.md](job-terminal-projection.md)；静态清单190源/191路由/263边界，其余原生请求/会话接线继续N4-03。

本批将正式报告worker及API复编译的Pandoc stdin解析、HTML/DOCX编译和版本探测接入持久子执行，父终态前还会等待断线残留调用；静态清单190源/191路由/262边界。准备请求仍待移出case写事务，PTY/viz/Agent等入口继续独立验收，见 [owned-pandoc-children.md](owned-pandoc-children.md)。

本批LaTeX的worker探测/正式编译和API独立复编译已接入API所属原生子执行：父attempt/nonce、放行前登记、实际退出核验和持久回执，双库各15项。机器清单现188源/191路由/256边界；其余探测、Pandoc、SyncTeX、PTY/viz及Agent仍逐项验收，见 [owned-native-children.md](owned-native-children.md)。

N4-03/04增量把handler.artifacts原生验证移出写事务，提交仍绑定原attempt快照并复核来源/输出/代次。Job取消先提交持久请求，再通知本机或队列；内部WSL及会话监督仍未全部接通，见 [publication-preflight.md](publication-preflight.md)。

N4-03首批已补MPI非root账户/namespace接线和真实ranks退出证据；185源/191路由/252边界。内部WSL编译、API复编译/探测、PTY/viz、Agent/CLI及其余旁路继续按 [native-entry-supervision.md](native-entry-supervision.md) 逐入口验收。

N4-02完成：通用执行范围增加Windows生命周期成员观察/累计对账和错误后备终止；185源/191路由/252静态边界。完整调用入口归属及子执行等待接线进入N4-03，现有内部WSL/最终复编译/MPI/PTY/viz不能因通用停止通过而自动宣称已覆盖。

N4-02首批：通用start_job接通根退出并行观察、同一次停止/升级、范围退出核验与attempt.process_stop持久结果；184源/191路由/251静态边界。N4-02剩余验收和N4-03旁路接线见 [process-tree-stop.md](process-tree-stop.md)。

N4-01：start_job统一使用固定启动门、实际Windows创建句柄/Job Object及WSL内部出生身份，running事务提交后放行。新增GET /jobs/{id}/process；当前183源/191路由/248静态边界。旁路WSL编译器/独立复编译/PTY/viz继续N4-03；完整停止与恢复继续N4-02/N5。

普通报告增量：全部HTML/DOCX和独立compose入口注册，移除report_worker兼容启动、同步直接写稿和进程内latest；工程与普通文档门禁分开验证，均保留持久版本。当前178源/190路由/245静态边界；静态分类计数不等同真实I/O数量。见 [ordinary-document-workflows.md](ordinary-document-workflows.md)。

LaTeX注册增量：全部编译入口持久幂等→冻结资源→固定worker/WSL原生编译→独立内容复编译→按主文件持久发布→PDF/SyncTeX/源码历史读取。当前178源/190路由/249静态边界；分类器包含字符串replace等保守命中，不等同于249项真实文件写入。strace记录用于依赖验证，不构成任意TeX/Lua安全沙箱。

独立compose增量：持久幂等准入 → 封存caseDict/后处理/网格与原稿 → 固定compose worker → 独立重生成正文和原字节备份 → 按输出分开发布head → 目标前置检查及幂等镜像。增加3个读取路由；静态边界计数仍为241，不代表原生I/O数量不变，详见 [report-compositions.md](report-compositions.md)。下文旧compose路径已由普通文档统一增量替代。

注册HTML/DOCX：workflow report.build或已有postprocess head的report:build → 确认后处理/网格质量+正文资源 → 固定Pandoc worker → HTML/DOCX真实内容复编译 → report publication/head → 兼容别名。未建立postprocess head的case也已统一注册；独立compose随后接通双通道版本，全部LaTeX入口随后注册。详见 [report-workflows.md](report-workflows.md)、[report-compositions.md](report-compositions.md) 与 [latex-workflows.md](latex-workflows.md)。

注册后处理：workflow_execution或postprocess:run → PostprocessHandler → solve/mesh publication+参数/参考封存 → 固定worker → 指标/图件独立复算 → postprocess publication → E确认。既有报告准入和收尾新增当前head门禁；下表旧postprocess入口仅用于未建立solve head的兼容case。详见 [postprocess-workflows.md](postprocess-workflows.md)。

注册串行求解：workflow_execution → SolveHandler → 显式工程输入+mesh publication → WSL solve_worker → 原生求解 → 日志/完整场验证 → solve publication → D确认。gzip网格只在新尝试内解压为规范网格后交checkMesh校验；无输入字典重写。详见 [solve-workflows.md](solve-workflows.md)。

注册网格增量：workflow_execution → MeshHandler.prepare → 封存case输入及几何publication → WSL mesh_worker → 真实blockMesh/checkMesh → 重新验证质量/边界/输出哈希 → 不可变mesh发布 → 当前生产版本C确认。原mesh:evaluate仍为兼容评估，注册head存在后不能用手工日志重评冒充专业发布；详见 [mesh-workflows.md](mesh-workflows.md)。

本轮后续修正：普通 Job、Redis run_job 与批次已使用完整 wait，关键 DB 失败不再吞掉，终态有条件更新；详见 [Job 收尾实现](job-finalization.md)。下表中“回调提前”是初始发现，已由这项修正替换；持久attempt、普通请求幂等与spawn前claim随后已接入，见 [job-execution.md](job-execution.md)；进程树、完整取消/发布代次与重启恢复仍未完成。机器清单已按修正后源码刷新，此前compose增量173源/186路由/241边界；默认几何版本化发布的进一步接线见 [geometry-publication.md](geometry-publication.md)。

审查日期：2026-11-02。源码路径相对 platform。本清单记录迁移前事实，后续代码实现应同步修订。机器清单 [execution-entry-inventory.json](execution-entry-inventory.json) 枚举 `server/app/**/*.py` 的路由、提交、进程、取消、受监督资源和文件提交边界，并记录源文件哈希。生成/核对命令分别为 `python tools/execution_inventory.py --write` 和不带 `--write`。它会捕获新增入口，不声称静态扫描能证明用户 shell 或 native 子进程的完整调用图。

后续N2提交增量：报告/LaTeX准备和发布、PPT/PDF及其他专业Job入队、批次manifest/index进入case锁；本地未claim queued项可取消，已claim不覆盖。双库各81项和分轮兼容证据见 [case-write-coordination.md](case-write-coordination.md)。派生与viz/terminal启动随后通过双库各93项；原生写、跨API持久会话、封存输入、持久发布和恢复仍待续。

发布输入后续接线：Job和内部queue_step可选择同case publication；封存到独立run并记录来源，current依赖在启动与收尾失效时拒绝成功，历史复算需显式策略；见 [publication-inputs.md](publication-inputs.md)。

注册工作流增量：workflows创建/显式派发 → workflow_execution → 静态handler.prepare → queue_step封存/绑定/预留 → commit → 原子claim → worker → handler产物验证/不可变发布 → 生产版本确认。6个新路由及原几何preview/export复用同一契约，详见 [registered-workflows.md](registered-workflows.md)。

## 归属与调用链

下表中的 case 路由均先经 `get_owned_case` 检查，Job 查询/取消经 `_get_job` 回查 case；保留工作区协作权限规则。组织级远程/SCNET 检查 admin，但当前并未绑定 case/Job，不能直接视为可信工作流步骤。服务内部入口必须接收已验证的主体、case 与 attempt，不能用“已有 Job ID”代替授权。

| 入口与调用链 | 输入 → 输出/事实源 | 当前取消/重试语义 | 迁移归属 |
|---|---|---|---|
| `routers/jobs.py submit_job → distributed_queue.enqueue` 或 `jobrunner.start_job` | command/runtime/预算，可选 solve run_dir → Job、日志、求解 evidence | 本地 proc.cancel 请求owned scope退出；持久身份/登记门已接通，温和停止/限时升级和累计退出核验已验收，旁路后代继续N4-03；Redis广播和正式重试API待续 | N1 claim/attempt，N3-04，N4，N5；shell 默认禁止自动重放 |
| `jobs.py submit_batch → batch_jobs.start_batch → _worker → _start_when_capacity → start_job` | commands/并发 → 每项 Job、`.vibeflow/job-batches/*.json` | 批次/单 case 取消 queued/running；完成回调早于最终持久化；重提生成新 Job，批次调度仍在内存 | N1-05，N3-07，N4-06，N5 |
| `geometry.py :preview/:export → _launch → start_job → geometry runner → artifact_publication.finalize` | caseDict/A-geo封存 → 独立run → 不可变scene/validation/toolchain、STEP/STL/manifest，持久head选择当前版本 | 可选幂等键；旧代次保留superseded历史，不覆盖新head；取消沿用通用Job，完整树/恢复待续 | N2-03/04/05首批已接通，见geometry-publication；N3-02注册调度接线已实现，N4/N5待续 |
| `geometry.py /repair,/msh-preview,/import-scene` | 用户几何文件/修复参数 → 修复件或只读预览 | 部分为同步本地计算；不是后台 Job，可被 HTTP 超时打断但不保证撤销 | N2 写协调，后续几何适配器；只读预览保持只读 |
| `mesh.py mesh:evaluate → evaluate_mesh_quality → write_quality_report` | checkMesh.log/当前网格/质量政策 → B-mesh/quality.json | 同步评估；旧通用Job入口保留；新增注册mesh.generate通过工作流执行真实blockMesh/checkMesh | N2 写协调，N3-03，禁止合成日志冒充专业成功 |
| `mpi.py mpi:run → start_job(runtime=wsl)` | runDir/solver/subdomains → decomposeParDict、processor*、重构时刻、日志 | nobody/保留UID私有namespace，双库真实ranks取消/限额/API猝死退出已验证；本入口尚未绑定注册 solve 的完整版本发布/恢复契约 | N3-07，N4-03，N5-05 |
| `udf.py :build/:integrate → _launch_async → start_job(wsl)` | C++/wmake/链接配置 → 动态库、controlDict、构建日志 | scaffold 同步写；构建走 Job，取消编译器树未统一；integrate 有工程写副作用 | N2-02，N3-07，N4-03；不盲目重放 integrate |
| `postprocess.py postprocess:run → start_job → postprocess_executor` | caseDict 后处理快照、runDir/场 → postprocess-{job}/metrics/manifest/图表 | executor 检查输入后直接替换 latest-postprocess.json；Job 取消不能回滚已提交指针 | N2-05/06，N3-05，N4-04 |
| `reports.py report:compose → compose_handler/compose_publication` | 封存普通/工程证据与原稿 → 9章Markdown/原字节备份/版本 | 全部入口持久Job、双通道head；固定生成时间与目标检查，兼容无后处理文档 | N3-06，N4进程树，N5崩溃对账，N6版本UI |
| `reports.py report:build → report_handler/artifact_publication` | 封存Markdown/图片及适用工程来源 → HTML/DOCX/manifest | 全部入口事务外真实复编译、锁内复核提交、持久代次和当前/历史；旧两代幂等键兼容，无内存latest | N2-05/06，N3-06，N4-04，N5-04 |
| `reports.py latex:build → latex_handler → registered_latex_worker → artifact_publication` | 声明范围内TeX/图片/参考文献/数据和工程证据 → PDF/SyncTeX/BBL/日志/历史源码 | 全部入口注册；独立原生复编译验证；按主文件持久head；源/上游变化、取消和迟到受围栏约束 | N4完整进程树，N5崩溃对账，N6历史阅读UI；不是任意TeX安全沙箱 |
| `reports.py ppt:build → start_job(ppt_worker)` | 校验过的结构化 slides/template/images → PPTX | 拒绝覆盖已有文件；输入资源/产物提交尚未统一快照 | N2 attempt/publication，N3-06；全功能 R4 |
| `reports.py pdf:number → start_job(pdf_number_worker)` | 源 PDF/输出路径 → 带页码 PDF | 不覆盖源和已有目标；运行途中目标竞争未统一 | N2 发布，N4；全功能 R4 |
| `reports.py synctex:query → latex_publication.query_synctex 或 synctex_service 兼容分支` | 当前/指定历史PDF/page/x/y → 原版本源码位置与当前匹配标记 | 有注册head时使用不可变PDF/原侧车/历史源码映射；无head旧PDF保留兼容查询；短时只读子进程 | N4监督，N6历史源码阅读；保持只读 |
| `flotherm.py :build-library/:convert → start_job` | JEDEC/ECXML/CSV/参数 → 模型库或转换几何/清单 | 部分 wsl、部分 argv worker；通用 Job 取消；inspect 同步读取 | N2 attempts，N4；全功能 R2 |
| `su2.py :solve → start_job(argv) → OwnedProcess` | 专用配置/网格 → DAT/FRD/STA 或 VTU/history；路径作为独立参数 | 普通/特殊文件名实际求解 4 项、SU2 取消/超时/owner 猝死 3 项通过，退出回执绑定 attempt | 继续其他专业故障和各自 handler；不能套用 OF 场校验，见 professional-entry-supervision.md |
| `paraview.py :run → start_job(pvpython/pvbatch)` | 脚本/输入文件 → VTP/PNG/数据 | 通用 Job 取消，任意脚本无安全自动重放策略 | N2/N4；全功能 R3 |
| `terminal.py WebSocket → NativeSession → OwnedTerminal → ConPTY/Linux PTY` | 用户交互/终端尺寸/case staging → PTY 输出、文件修改 | 持久归属和两级登记、同根重连、实际退出及 S3 收尾；不属于普通 Job | 双库各44/S3可控各9；完整预算/活动/停机/N5继续，见 owned-pty-sessions.md |
| `viz.py session create → NativeVizSession → OwnedProcess → viz_kernel` | case 资源/会话 → kernel/WS 场景/日志 | 持久归属、根登记后放行、首帧实际身份、停止和 S3 收尾；端口竞争已验证 | 双库各33/S3可控各9；不把查看关闭当求解取消，完整预算/跨主机路由/N5继续 |
| `agent.py messages/goal → 原子工具审批 → execute_case_tool → agent_shell` | 已审批命令唯一绑定ToolCall/NativeRequest；cmd/PowerShell/WSL → 结果及文件副作用 | 不建Job，登记实际根后放行；取消/断线等待所属树和S3收尾；不跨重启重放 | g1双库32/S3可控9；完整N4-04/N5/R5继续 |
| `routers/codex → session_requests/turns/sessions → CodexStdioClient → OwnedStdio → OwnedProcess` | Windows/WSL 原生启动/回合按用户、case、conversation 绑定；幂等启动、审批、resume/fork，不建 Job | 实际登记、会话执行权、所属目录初始化、原生退出及 S3 收尾；托管 API 凭据和网站身份分开，未知结果不重放 | 新入口 WSL 实际10项、初始化专项9项通过；正式部署、预算/停机/N5继续，见 native-cli-startup.md |
| `remote.py remote:execute → remote_exec.run_remote → asyncssh` | admin 的 SSH profile/host/command → stdout/exit | 有超时，未创建 case Job/attempt；远程进程退出不能由本地 PID 推断 | 外部 handler，R6/I；默认人工重试，无自动资源创建 |
| `scnet.py /{operation} → require_confirmation → ScnetClient.execute` | admin 临时凭证/操作参数 → 远端资源/文件/作业 | 部分资源操作明确 confirmed；非本地 Job；重试可能重复创建/计费 | R6；先远端幂等/资源 ID 对账再接入工作流 |
| `runtime.py probes → NativeRequest/RequestExecutor; cli_discovery.py → 文件发现` | 可执行路径/运行时 → 可用性/版本 | Python/WSL 探测按用户所属、case为空；CLI 正式调用未启用 wsl_runner 分支；Setup 写独立 runtime config | 双库探测各25；不纳入工程阶段证据，新增 CLI WSL 探测必须接入监督 |
| `codex_sessions → OwnedStdio → 已登记 Linux bridge → codex_wsl_state.initialize` | 托管账户配置 → Linux 私有目录；固定非 root 初始化、15秒期限 | 两级登记后初始化，同一 scope 停止；prepared 后复验配置/权限/owner，再单独放行 CLI | 专项9、双库各16、实际CLI10通过；残留目录/未知持久终态继续N5，见 owned-cli-state-preparation.md |

## 所有取消入口和恢复入口

`POST /jobs/{id}:cancel`、批次 `:cancel`、`runtime.py` 的 case 进程取消最终进入 `JobProcess.cancel` 或 `supervisor.cancel_case`；监督器另有 batch/agent/goal/terminal/viz callback。Redis listener 调用 ACTIVE proc.cancel。墙钟/日志限额已通过OwnedProcess停止与退出核验；内部编译及会话旁路尚未全部接线。以上必须统一保留“请求取消”和“进程已停止”两个事实，不能将 asyncio task.cancel 当成 native 子进程已退出。

Agent `/messages:cancel`、Goal 状态操作、终端/可视化关闭各有会话语义，不应改成取消整个 case。`main.startup` 目前把所有 queued/running Job/ToolCall 标 interrupted，另调用 Agent recovery；没有通用 Job retry/resume 端点。现有前端重新提交命令是新执行，不是假定续算。后续提供 retry/new attempt、rerun/new run、resume/验证过的 checkpoint 三类动作，旧记录不改回 queued。

## 旁路写入与发布边界

N2增量：文件/字典/上传与Agent提案已接入case协调器；Agent memory自动及批准调用也在锁内读取、合并、写回。copy_case持有来源锁，copy_workspace按ID锁定全部来源后创建目标，再以哈希复核来源；重命名/移动同步协调。附件/用户消息已在case锁内检查配额和提交，备份按ID锁全部来源并hash复核，恢复包在新目标分配前校验。回收站及成员变更先锁工作区再锁case，删除/恢复还持有跨进程清单锁；整区复制/备份在工作区锁后枚举成员。Job提交/领取及协作写入在case锁后重新核验回收站状态。geometry修复/Gmsh、mesh评估、UDF、MPI准备、postprocess配置/提交、report compose和CAD WS现已进入case锁；geometry preview/export入队同样协调，线程取消先等文件操作结束，Job启动在commit后。报告构建/其他专业Job、批次manifest、工况派生和viz/terminal启动也已有后续协调证据；原生worker持续写和跨API会话不能据此视为已统一，详见 [case-write-coordination.md](case-write-coordination.md)。

- `files.py` 普通保存、上传/分片完成、删除、caseDict/块/extensions/errors/units/advance，Agent proposal apply，`casedict.py` 与失败 fallback，工作区 import/copy/restore，几何 repair/UDF scaffold/integrate、mesh:evaluate/report:compose 均是 case writer。N2 必须覆盖；只锁报告 finish 不够。
- `storage.py` 的 fs 临时文件+replace 和 S3 put、staging sync 是底层提交；不可把它们分别成功解释为数据库和多文件联合事务成功。
- 已注册几何、网格、求解、后处理、报告与LaTeX在独立attempt中生成，通过ArtifactPublication验证并提交当前指针；兼容入口及PPT/PDF等其余专业worker仍须按台账继续迁移，不能用局部注册覆盖率代表全入口完成。
- `eventbus.publish` 是内存广播；`events.py` 的文件快照轮询/索引补偿是现有兜底。持久 outbox 只能在状态事务提交后投递；索引失败不得撤销有效求解。
- 用户脚本、MPI/OpenFOAM/TeX/native 库可能产生机器清单不可见的子进程/写入。attempt 工作目录、进程身份和运行时树验收共同约束，不能仅靠此表承诺完全隔离。

## 迁移检查

每接入一个 handler，更新本表与机器清单，记录输入封存、原始输出、专业校验、活动指针、取消及恢复政策；重跑对应真实回归。Redis 的重复投递/确认、S3 同步冲突、同步 API 与工作流 API 都必须使用同一核心状态和写协议，不保留永久旁路。全部外部付费/凭证操作仍以原确认契约为准。

本地 CalculiX 现由 `calculix.solve@1 → 私有 worker → 持久化原生子执行 → 不可变发布 → 有条件更新原路径` 接入，见 [CalculiX发布](calculix-publication.md)。上表原直连描述仅保留 SU2；CalculiX 的 S3 路径尚未迁移。

2026-11-07：`TerminalSession → terminal.export NativeRequest → process_gate 目录描述符传递 → sandbox_export_native → 文件事务/远端staging → terminal.export_published` 已接入。只有完整进程退出、导出和发布均有证据才允许新沙箱 closed；原历史不追认。强制/正常退出、并发源冲突与真实S3见 [sandbox-export-lifecycle.md](sandbox-export-lifecycle.md)，持有者猝死和完整资源矩阵继续。

2026-11-07：新沙箱启动门在用户代码前创建受节点额度约束的128MiB ext4镜像，在私有挂载命名空间挂载并把目录绑定交给namespace/nobody执行。普通导出和发布仍受旧的分别核验协议约束；API猝死后只读镜像验证通过，但恢复操作入口、卷清理和跨WSL重启仍待，见 [sandbox-persistent-volume.md](sandbox-persistent-volume.md)。
