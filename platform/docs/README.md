2026-11-16，主线 **48/53，剩 5 项**。一键启动切换到实际模型配置，Docker worker 改用正式执行器，Redis 启用认证健康检查和持久化。 [当日交付记录](delivery-status-20261116.md)

# VibeFlow Platform — 文档目录

2026-11-07：[WSL作用域控制通道及持久启动实现绑定](native-scope-control.md)。控制通道与双库记录/篡改检查通过，本轮真实DeepSeek4次17053 tokens、模拟0；实际cgroup身份/回收尚未接入请求。主线33/53、剩20，资源11/17、剩6，295项最终验收保持。

2026-11-07：[Windows宿主创建时原子加入Job](windows-job-creation.md)。关闭创建后再分配的空窗，实际进程与双库恢复链路通过；本轮真实DeepSeek4次17115 tokens、模拟0。WSL作用域持久恢复继续，主线33/53、剩20，资源11/17、剩6，295项最终验收保持。

2026-11-07：[启动内核作用域、实际内核/门证据及七项接入任务](native-cgroup-startup.md)。本轮真实DeepSeek 2次、8530 tokens、模拟0；cgroup持久恢复尚未接通，主线33/53、剩20，资源11/17、剩6，295项最终验收目标保持。

2026-11-07（准备取消与存活API）：真实复现已终止请求被存活API误阻塞，新增terminal_request所有权隔离依据，仍要求已知子进程实际退出；修复准备取消后786432bytes节点预留残留。SQLite/PG最终代码各3项基础设施和1项证据故障检查通过，活动所有者双API回归5项通过。本轮真实DeepSeek4次16938 tokens，模拟0；首个验证脚本PID假设失败已单独保存。出生身份缺失和未知WSL窗口仍拒绝。主线33/53完成、剩20；资源11/17完成、剩6；295项最终验收目标保持。 见[准备取消与预留释放](sandbox-image-live-owner.md)。

2026-11-07（清理启动观察与双库取消）：新增持久启动意图和created/host_ready/native_ready实际观察，created与native_ready之后API退出、未完成登记时的独立退出对账已通过；完全缺失出生观察或缺少WSL身份仍拒绝。SQLite/PostgreSQL双API竞争、连续三次取消、实际停止后接管和唯一回执通过；最终代码另复核PG。本轮真实DeepSeek4次、17092 tokens、模拟0，严格纯字符串格式仍未通过。未知WSL身份、PG启动对账、原生定点故障/权限/重启等继续；主线33/53完成、剩20，资源11/17完成、剩6，295项最终验收不缩减。 见[启动证据与双库验收](sandbox-image-startup.md)。

2026-11-07（原镜像清理中断恢复及界面）：已登记清理者的实际所有者/子进程退出核验、独立对账凭证和原日志保留已实现；七处真实API退出、9项基础设施和1项证据故障检查通过。新增界面确认/断网重开只查询/显式重试，实际S3联动删除及13项镜像浏览器检查通过。本轮2次真实DeepSeek调用8580 tokens，模拟0；严格只回复字符串仍未通过。未登记身份窗口、PG/竞争/取消及原生定点故障等继续；主线33/53完成、剩20，资源11/17完成、剩6，295项最终验收目标保持。 见[清理中断恢复与界面](sandbox-image-reconciliation.md)。

2026-11-07（原镜像注册清理后端）：新增terminal.cleanup原生请求、固定外层gate核验与volume.ext4删除、持久意图/完成回执及恢复互斥。FS/S3真实删除、完整镜像哈希变更拒绝、实际flock/只读loop占用拒绝、元数据/快照保留通过；新增6项基础设施、6项协议/故障、双库12项schema检查。实际删除后真实DeepSeek读回恢复文件，2次调用8513 tokens，模拟0。清理者猝死独立对账和界面入口待完成。新迁移c6a1de7402ef仅隔离库验收，开发库未迁移；主线33/53、剩20，资源11/17、剩6，295项最终验收继续。 见[注册原镜像清理](sandbox-image-cleanup.md)。

2026-11-07（暂存准备中断/并发取消）：清理意图改为先提交哈希，再写清单；只续写完整原暂存能重建出的已知前缀，未知字节不覆盖。9个实际API退出位置通过；SQLite/PostgreSQL两API竞争及连续取消3次，均先排空I/O再接续并只提交一次回执。新增14项实际基础设施、3项协议/故障检查，2次真实DeepSeek调用8397 tokens，模拟0；PG首次遗漏迁移失败保留，修复后复验通过。镜像/其他副本清理与全量资源边界继续，主线33/53、剩20，资源11/17、剩6，295项最终验收继续。 见[准备中断与并发取消](sandbox-staging-restart.md)。

2026-11-07（原始S3暂存清理）：已实现持久清理意图/回执、逐项核验删除、实际API删除中途退出后重试与界面确认；未知/变更文件阻止清理，快照及原镜像保留，成功后解除这份暂存的管理阻塞。新增3项实际基础设施、4项协议/故障和13项浏览器检查通过；清理后恢复文件由真实DeepSeek读取续答，2次真实调用8422 tokens，模拟0。原镜像、其他暂存/恢复副本清理和剩余故障矩阵未完成；主线33/53、剩20，资源11/17、剩6，295项最终验收继续。 见[原始暂存清理](sandbox-staging-cleanup.md)。

N6-01已按原条件完成：case稳定ID/序号、分页游标、跨API实时补拉及重连后数据库状态对账；迟到成功不能覆盖当前失败，刷新不新增执行。双库各10项、真实浏览器4项、旧事件/时间线和outbox退出重投兼容及前端构建通过。主线20完成/14进行中/19待开始，剩33；全功能295项严格完整验收仍0。 详见[持久事件补拉](durable-event-replay.md)。

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

N4-03.h：FloTHERM复制编译专项5项通过，实测cc1plus在副本目录运行，错误登记/取消/超时/API猝死保留原始及复制源码；并发与重复取消不改写停止回执，新API读取四项历史且无重放。UDF/FloTHERM原同目录夹具各4项复验通过。本批仅测试/文档，53/295、11完成/16实施中/26待实施及head3d1c98e2b754/35表不变。 详见[专业进程故障验证](professional-process-faults.md)。

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

> 新接手先读 [HANDOVER.md](HANDOVER.md)；新环境从 [dev-setup.md](dev-setup.md) 开始。仓库总入口为 [../../README.md](../../README.md)。

## 当前操作文档

| 文件 | 内容 | 更新时机 |
|---|---|---|
| [native-entry-supervision.md](native-entry-supervision.md) | N4-03全入口任务、MPI非root实现与真实ranks清理证据 | 原生入口接线/验收变化 |
| [publication-preflight.md](publication-preflight.md) | 事务外原生验证、所属快照复核及持久取消/发布竞争 | 验证与取消协议变化 |
| [owned-native-children.md](owned-native-children.md) | LaTeX原生子执行归属、持久登记、停止核验与剩余入口 | 原生执行协议变化 |
| [owned-pandoc-children.md](owned-pandoc-children.md) | Pandoc stdin与所属子执行、断线等待、短命成员核验和剩余请求任务 | 文档原生执行协议变化 |
| [owned-cli-turns.md](owned-cli-turns.md) | 原生回合/SSE、审批/计划问答、共享执行归属、显式续接和交付证据 | CLI回合与账户/WSL/UI后续任务 |
| [owned-cli-sessions.md](owned-cli-sessions.md) | CLI持久会话、正式API、私有账户目录和S3收尾 | 会话基础；后续原生回合见owned-cli-turns |
| [owned-codex-transport.md](owned-codex-transport.md) | 实际Codex协议、所属stdio通信、22项专项及g2b任务 | CLI客户端基础与产品持久接线 |
| [owned-agent-shell.md](owned-agent-shell.md) | Agent shell工具归属、原子审批、实际退出与S3收尾 | API Agent执行与后续CLI |
| [owned-viz-sessions.md](owned-viz-sessions.md) | viz持久归属、首帧就绪、实际退出与S3收尾 | 可视化会话与监督 |
| [owned-pty-sessions.md](owned-pty-sessions.md) | PTY持久归属、输入背压、实际退出与S3收尾 | 终端/会话生命周期 |
| [owned-runtime-probes.md](owned-runtime-probes.md) | 用户所属全局Python/WSL探测、实际退出与降级证据 | 能力探测/请求范围 |
| [owned-synctex-requests.md](owned-synctex-requests.md) | 只读SyncTeX请求、冻结来源、原生退出与历史映射 | PDF回跳/请求生命周期 |
| [document-admission.md](document-admission.md) | 正式文档锁外准备、来源重验、HTTP与工作流取消 | 报告/LaTeX/工程报告提交 |
| [native-request-scopes.md](native-request-scopes.md) | 独立准备请求归属、持久身份/租约、双库验证与业务整合任务 | 提交前解析/探测接线 |
| [job-terminal-projection.md](job-terminal-projection.md) | 逻辑终态、兼容产物收尾、跨API SSE与恢复边界 | 作业结果/日志协议变化 |
| [HANDOVER.md](HANDOVER.md) | 当前架构、代码入口、契约、恢复与接手检查单 | 重大架构/契约变化 |
| [next-stage-mainline-plan.md](next-stage-mainline-plan.md) | 下一大阶段 53 项任务、24 个故障场景、依赖、迁移和回退 | 每个实施/验收节点 |
| [mainline-task-status.json](mainline-task-status.json) | 53项执行状态、已完成证据、剩余工作和24个故障验收状态 | 每次实际验收后 |
| [mainline-baseline.md](mainline-baseline.md) | 源码版本、哈希、独立恢复和阶段回归 | 每个可回退里程碑 |
| [execution-entry-matrix.md](execution-entry-matrix.md) | 全入口调用链、归属、取消/发布现状与迁移去向 | 每次执行入口变化 |
| [workflow-execution-contract.md](workflow-execution-contract.md) | 状态/实体/API/错误码、幂等、确认、恢复与部署契约 | 对应实现定稿后 |
| [workflow-scheduler.md](workflow-scheduler.md) | 依赖调度/失败传播/工程确认核心与双数据库证据 | 调度与确认实现变化 |
| [case-write-coordination.md](case-write-coordination.md) | 跨进程 case 锁、revision/hash、Agent/上传冲突与剩余入口 | N2写入与一致性变化 |
| [attempt-workspaces.md](attempt-workspaces.md) | 输入封存、独立运行目录、输入保护和重试来源 | 尝试隔离变化 |
| [geometry-publication.md](geometry-publication.md) | 几何默认隔离执行、持久发布代次、当前/历史读取及失效 | 几何发布与消费者变化 |
| [publication-inputs.md](publication-inputs.md) | 下游引用发布版本、混合输入封存、current策略与重试来源 | 发布依赖和步骤输入变化 |
| [mesh-workflows.md](mesh-workflows.md) | 注册网格执行、质量政策、边界/版本证据与C确认 | 网格/求解接线变化 |
| [solve-workflows.md](solve-workflows.md) | 注册串行求解、场/时刻/残差验证与D确认 | 求解执行与发布变化 |
| [postprocess-workflows.md](postprocess-workflows.md) | 后处理复算、图件、readiness和E确认 | 后处理与报告门禁变化 |
| [report-workflows.md](report-workflows.md) | 注册HTML/DOCX、来源绑定与持久版本 | 报告编译与消费变化 |
| [ordinary-document-workflows.md](ordinary-document-workflows.md) | 普通/工程报告统一入口、固定时间、旧证据门禁与幂等兼容 | 普通文档与工程来源衔接变化 |
| [report-compositions.md](report-compositions.md) | 独立组稿、原字节备份、覆盖保护和历史 | 草稿生成与覆盖规则变化 |
| [latex-workflows.md](latex-workflows.md) | LaTeX注册编译、动态依赖、按主文件版本与历史源码定位 | LaTeX编译、发布和阅读变化 |
| [registered-workflows.md](registered-workflows.md) | 注册处理器、工作流API、专业步骤与发布契约 | 专业步骤接入变化 |
| [process-tree-stop.md](process-tree-stop.md) | 进程树温和停止/升级、退出核验、持久清理结果和剩余验收 | N4-02停止实现变化 |
| [process-identities.md](process-identities.md) | 持久OS出生身份、Windows/WSL启动门、登记顺序和只读探测 | N4进程监督变化 |
| [job-execution.md](job-execution.md) | Job幂等提交、原子claim、持久attempt、重试谱系与验收边界 | N1执行服务变化 |
| [job-finalization.md](job-finalization.md) | Job完整收尾、终态保护、故障验证与剩余边界 | N1收尾实现变化 |
| [workflow-persistence.md](workflow-persistence.md) | 新增10表/迁移、状态CAS、事务outbox、重启证据与开发库保护 | 持久执行实现变化 |
| [full-function-delivery-plan.md](full-function-delivery-plan.md) | 全部 295 项任务、136 个未闭合审计项、历史决策复核和 R0–R7 | 全功能范围或状态变化 |
| [full-feature-tasks.json](full-feature-tasks.json) | 与全功能文档一致的 295 项机器台账、主 WP、波次和验收包 | 与逐项文档同步 |
| [dev-setup.md](dev-setup.md) | 新机器依赖、配置、启动、WSL 与迁移步骤 | 环境或启动方式变化 |
| [database.md](database.md) | 代码22表、开发库12业务表、新迁移、FTS、事实源与备份 | 模型或迁移变化 |
| [api.md](api.md) | 当前 API、在线 OpenAPI、SSE/WS、错误与通道限制 | 每个端点/协议变化 |
| [testing.md](testing.md) | offline/core/cae/execution/writes 分组、192项 Python 入口、前端、E2E、SKIP 与副作用 | 测试或前置变化 |
| [deployment.md](deployment.md) | 配置、候选部署、健康、备份、升级与生产验收 | 部署形态变化 |
| [security.md](security.md) | 当前威胁模型、归属、命令执行、token、密钥与上线检查 | 安全边界或策略变化 |
| [third-party.md](third-party.md) | 原版提取资产、第三方组件、模型权重与许可证核查 | 资产/版本/分发方式变化 |

## 方案、差距与历史

| 文件 | 内容 | 使用方式 |
|---|---|---|
| [parity-checklist.md](parity-checklist.md) | 与桌面版的当前功能差距（领域摘要） | 功能模块完成或缺口变化时更新 |
| [feature-gap-audit.md](feature-gap-audit.md) | 原版 vs 当前 Web 的 295 项证据台账、边界和遗漏复核 | 全量审计、功能或证据变化时更新 |
| [p0-remediation-plan.md](p0-remediation-plan.md) | P0 行为缺陷的 TDD 修复步骤、验证和当前状态 | 每个 P0 修复节点更新 |
| [p1-udf-plan.md](p1-udf-plan.md) | UDF D1–D5 垂直切片：文档、编译、加载、算例集成与验证 | 每个 UDF 阶段更新 |
| [implementation-master-plan.md](implementation-master-plan.md) | 历史总体设计与 295 项 WP00–WP17 主归属；当前细节见两份新计划 | 架构、依赖或主 WP 映射变化 |
| [plan-cs-to-bs.md](plan-cs-to-bs.md) | 批准的 C/S→B/S 目标方案及当前实现偏差 | 方案变更时；不要当作现状清单 |
| [mechanism-audit.md](mechanism-audit.md) | 2026-10-21 原版机制审计与当前缺口复核 | 新审计或缺口关闭时更新 |
| [hardening-2026-10-24.md](hardening-2026-10-24.md) | 归属安全、caseDict、Alembic 加固实施记录 | 专项历史档案 |
| [progress.md](progress.md) | 当前状态和按日期追加的变更/验证日志 | 每个任务与验证节点更新 |
| [docker-repair.md](docker-repair.md) | 本机 Docker/WSL 故障记录和候选部署产物 | 环境变化时更新 |
| [shots/](shots/) | 浏览器 E2E 截图证据 | 每次 E2E；需实际查看 |

## 推荐阅读顺序

1. [HANDOVER.md](HANDOVER.md)：知道项目是什么、代码在哪里、哪些契约不能破坏。
2. [dev-setup.md](dev-setup.md)：创建环境并启动。
3. [testing.md](testing.md)：按改动选择验证，不依赖历史“全绿”。
4. [next-stage-mainline-plan.md](next-stage-mainline-plan.md) 与 [full-function-delivery-plan.md](full-function-delivery-plan.md)：确认近期任务和全量范围；证据回查审计台账。
5. 改动数据库、部署或安全边界时，分别必读对应专项文档。

## 速览

- **目标**：VibeFlow Studio 0.11.0 桌面版（Qt+ParaView）→ B/S Web 版。
- **技术栈**：React 19 + TS + Vite + dockview｜FastAPI + Alembic｜PostgreSQL｜本地 fs（S3 备用）｜WSL Ubuntu/OpenFOAM｜VTK/trame。
- **状态**：核心闭环已实现；当次验证与未执行项以 `progress.md` 为准，目标方案不当作当前实现。
