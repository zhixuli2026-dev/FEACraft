# N3-07 类型工作流批次（实现与验收进行中）

这是接续UDF实际文件查询子阶段的实现笔记，不是已交付声明。当前36/53、剩17项，原四条remaining和295项目标不变。

2026-11-09：已加入workflow_batches、workflow_batch_members和f9d4a10735bc增量迁移，成员以run/case复合外键绑定。临时SQLite/PostgreSQL各7项真实迁移/约束/旧数据保留/非空拒绝回退检查通过，模型0、模拟0；临时PG已删除。证据tmp/workflow-batch-schema-yvb9ju8w/result.json。先行nzeliduh的未知模式夹具长于VARCHAR(8)，PG先报长度错误而不是预期CHECK异常；改为7字符未知模式，保留原失败和当时入口源码，没有修改产品拒绝条件。

核心服务及POST/GET /workflow-batches、POST /workflow-batches/{id}:cancel已接入。独立几何成员、整批回滚、未知成员拒绝、同键重试/异内容拒绝、并发上限1、两份实际STEP、独立B确认和跨用户保护已在FS/PG与S3/PG通过；证据分别为tmp/live-cli-startup-4xjcd0fp/workflow-batch.json和tmp/live-cli-startup-0oo17zcn/workflow-batch.json。模型回合仅走live_llm_*的真实服务；数据库迁移检查单独归为纯协议。本批核心实现已归档至platform/backups/mainline-N3-07-typed-batch-core-20261109.zip；完整批次和N3-07尚未关闭。

现状：jobs:batch持久记录命令列表，不能表达成员的正式目标/MPI/UDF模式。WorkflowRun/Step已有正式几何、网格、串行/MPI/UDF、明确确认、重算和恢复。Job delivery已在claim之前按所属算例和计费用户串行核验容量，未知停止继续占用容量；专业handler的bind提供持久投递意图。

首先实现独立算例的正式工作流批次，不改写旧命令清单为虚构工作流。每个成员固定case_id、run_dir、target、expected_revision、专业参数和预算；持久父批次、成员key/次序、run_id及来源摘要。使用已有create_run生成完整类型计划，所有成员校验和创建在一个事务完成；只有完整提交后才能派发。任一成员非法不得留下半批。请求带幂等键，同键同请求返回原成员，不随最新revision重建；同键异请求拒绝。

归属：调用者必须有全部成员的写权限，各成员归属于同一计费用户。父批次只对原创建者可见；单个工作流仍依照原case权限读取，但不能借此看到其他成员信息。提交按case_id排序取得控制围栏，再用所属用户行串行核验并发；沿用case→user锁顺序，不在新路径引入反向顺序。

容量：批次上限约束真实原生作业，不把等待人工确认的成员计作运行进程。claim在已有用户容量检查下，按持久run成员关系计入所有当前占用Job，包括收尾和未知停止；以后步骤的Job同样继承限制，不能只约束首个Job。Redis和本地执行使用同一SQL判定。投递模式由配置确定并在父批次固定，不能由客户端任意改写。

取消：按固定成员和有序case围栏，在同一提交记录父取消和各未完成run/attempt取消。跨API重复取消不产生新run或Job。提交后唤醒本地拥有者；Redis拥有者通过正常租约心跳读取SQL取消，控制请求不等待Redis连接；API退出后由持久取消/投递对账继续，不能仅靠内存BatchState。成员失败默认保留其他成员独立结果；不自动补建失败成员。

真实验收：先在临时SQLite/PG做半批回滚、幂等、归属、同用户/跨批次上限和重复取消协议，再用实际几何/网格/MPI/UDF成员跑原生链。另以自建Redis和两个真实worker验证额度、取消、API/worker退出后续接、旧成员不重放；真实模型证据回读与原生/协议测试分别记录，不运行历史模拟LLM脚本。

仍须处理的原入口问题：同一case的多个C-run目录共享case级工程阶段与mesh/solve发布指针；不能把“不同run_dir”当作完整隔离。第一步的独立case批次不宣称解决同case多工况全部来源策略。原同case派生/命令入口保持，接下来需要以原多工况语义完成来源和桥接审计，不能在N3-07整项关闭时悄悄删掉这一条。

本批合计4次真实DeepSeek、17132tokens、模拟0；两项实际工程链、14项纯SQL检查分别记录。全部源码SHA与AST核验见tmp/mainline-N3-07-typed-batch-core-evidence/validation.json。下一步运行中批次取消及跨API恢复，然后专业成员与真实Redis矩阵。

## 运行中取消与新API明确恢复（几何成员）

2026-11-09（批次运行取消/明确恢复）：真实CadQuery运行成员与未领取成员，FS/PG和S3/PG跨API并发取消、单次版本、原进程出生身份退出、排队不启动、旧发布/场景保留通过；停止原API后，新API明确新批次完成两份实际STEP与B确认，旧历史/成员/幂等键保留，S3独立空缓存。新增4次真实DeepSeek、17272tokens、模拟0；首轮过早读取停止证明失败保留。主线36/53、剩17，继续Redis与专业成员及同case完整来源，原四条remaining不变。见[批次证据与后续](typed-workflow-batches.md)。

证据：tmp/live-cli-startup-x0t7d1ap/workflow-batch-cancel.json、tmp/live-cli-startup-qw3vcch2/workflow-batch-cancel.json。第一轮o2jbo29u直接索引尚为None的公开process_stop而失败；修正为等待attempt的cancelled终态和已验证停止凭据，再检查原生出生身份已退出。产品代码没有放宽取消或停止判定，原失败和执行时入口源码保留。

验收通过停用观察API的真实派发扫描，选定源API拥有几何进程，另一API仅提交SQL取消；原生脚本先实际构造CadQuery形体，再在测试拥有的路径等待释放，使取消落在实际导出体内。源API在已验证排空后终止；本批不宣称运行中拥有者猝死或Redis worker故障矩阵已完成。模型为独立实际证据回读，不宣称模型自主规划了所有工程回合。3个临时PG已删除、1个自建S3服务已停止，所属API/驱动只读检查无残留。

当前子阶段源码platform/backups/mainline-N3-07-typed-batch-cancel-20261109.zip，全部SHA/AST及原Python源码未变检查见tmp/mainline-N3-07-typed-batch-cancel-evidence/validation.json。

## 实际Redis与双worker（几何成员命名场景）

2026-11-09（类型批次Redis命名场景）：自建Redis实际重启/队列清空后，由原SQL意图恢复同一Job/attempt，双真实arq worker、worker重启后不重放、用户上限1/批次上限2及用户上限2/批次上限1、其他批次推进和真实运行取消在FS/PG与S3/PG通过。含先行FS共6次真实DeepSeek、25762tokens、模拟0；初次S3队首等待超时未归因，后续成功不等于已修复。主线36/53、剩17；继续MPI/UDF混合成员、同case来源和原409。见[证据与边界](typed-workflow-batches.md)。

当前入口live_llm_workflow_batch_redis_acceptance.py --source-profile-id 26 --postgres，可加--s3；支持模块workflow_batch_redis_support.py。Redis由受管WSL进程启动，核对其新建PID文件与实际INFO身份，重启不保存RDB/AOF，实际出生身份和停止证明均保留。没有使用开发Redis或修改开发数据库。前缀配置在API导入前生效，公共真实模型验收辅助入口增加可选、限四个Redis变量的环境覆盖；未提供时保留原前缀行为。产品批次及原生代码未修改。

当前版本FS证据tmp/live-cli-startup-3sdfzdkb/workflow-batch-redis.json，S3证据tmp/live-cli-startup-cavrpee6/workflow-batch-redis.json。先行FS的tat25gp7保留当时入口快照，版本早于新增只读队首诊断，不称逐字节同一入口。三个通过场景共12个真实worker进程均正常退出；含失败场景共7次Redis启动/停止均核验，4个临时PG已删除、2个自建S3服务已停止，相关API/worker/驱动检查无残留。模型仅独立回读实际证据，原生执行与模型回合分别记录。

失败保留：rg6s9rsm在服务启动前遇到Windows不能跟随包内Linux软链接；实际readlink和Redis 7.0.15版本探针确认程序可用，改在WSL预检。t_xhgxvj在未启动worker的S3场景等待队列长度恰为1超时，未保存当时实际长度和资格，不能确定原因；增加只读资格/占用/队列诊断后双后端通过，没有把首轮标成修复。该观察继续纳入N7-01故障矩阵，原N3-07四条remaining不删减。

本批仍未验收MPI/UDF成员、运行中worker猝死或同case多C-run完整来源策略。当前源码platform/backups/mainline-N3-07-typed-batch-Redis-20261109.zip，哈希/AST与证据汇总见tmp/mainline-N3-07-typed-batch-Redis-evidence/validation.json。

## 混合专业成员与存储准入

2026-11-09（MPI/UDF混合批次）：当前修复版FS/PG与S3/PG均完成9个真实Redis worker原生作业、两子域MPI、wmake/CDLL/UDF、400单元速度场及实际后处理，终止原API后不重放求解并明确D/E接续。修复成果归档/日志/复制遇到账户结算争用的直接失败，真实锁/账户预留/原时限/真实额度不足专项通过。含修复前FS共6次真实DeepSeek、25972tokens、模拟0；两轮S3失败和证据不足边界保留。主线36/53、剩17，N3-07继续同case来源及取消/恢复，原409未关闭。见[修复与证据](execution-storage-admission.md)。


2026-11-09（同case MPI批次）：原jobs:batch新增注册MPI操作，同case两工况/两真实Redis worker并行、完整封存与跨目录真实查询、缺失输入变更、未读文件变化、整批回滚、独立结果及重启幂等，FS/PG和S3/PG通过。新增跟踪后的MPI运行取消/明确恢复、320附加输入双后端及S3混合MPI/UDF链通过。累计14次真实DeepSeek、60408tokens、模拟0；另有纯协议9项与实际S3全量流式摘要/版本ABA/损坏/故障排空检查。五轮失败保留。主线36/53、剩17；UDF/串行同case成员、专业批次取消/恢复和原409继续。见[实现与边界](same-case-mpi-batches.md)。


2026-11-09（同case UDF批次）：原jobs:batch接入udf.integrate，按kind严格区分参数，保持旧commands/MPI摘要。FS/PG与S3/PG真实wmake、CDLL、双Redis worker及两个400单元工况通过，加载日志分目录；单成员与共享输入变更、缺失文件新增、未读文件变化、原子回滚及API重启幂等均有证据。正式UDF工作流双后端的固定库/网格、原始控制保留、D/E实际提取、改源码新ELF与前缀复用通过。新增8次真实DeepSeek、34466tokens、模拟0；模型仅独立回读。首次测试路由404保留，源码及清理核验已归档。主线36/53、剩17，N3-07仍未关闭。见[实现与证据](same-case-udf-batches.md)。


2026-11-09（同case串行批次）：注册solve.openfoam.prepared复用原串行worker，FS/PG与S3/PG双Redis worker、两个400单元工况、输入隔离/共享include/缺失文件/未读文件/原子回滚通过；明确同目录重跑包含完整原封存、实际跟踪私有旧结果清理和新版本，重启保持原批次/幂等。原正式串行/MPI/下游链兼容通过。新增6次真实DeepSeek、25828tokens，模型仅独立回读，模拟0。两次测试取数失败保留。主线36/53、剩17，完整取消恢复/worker猝死/原409继续，N3-07仍未关闭。见[实现与证据](same-case-serial-batches.md)。


2026-11-09（专业批次取消与明确重跑）：FS/PG与S3/PG真实长MPI/UDF整批取消、等待串行不启动、所有观察出生身份退出、旧成果/历史/幂等保留通过；原API退出后新API明确三模式400单元新批次、D/E及实际后处理通过，S3独立空缓存。提交后清理真实SQL锁缺陷、MPI父进程重写旧日志来源及实际文件盘点原子替换缺陷已修复并分项验收。新增8次真实DeepSeek、34460tokens，仅独立回读，模拟0；纯文件系统故障另列。七轮组合失败与一轮S3专项错误保留；第七轮503底层原因未记录，不反推归因。主线36/53、剩17，worker猝死、同case准备批次取消、新跟踪范围及原UDF409继续，N3-07不关闭。见[实际范围与失败](professional-batch-cancel-recovery.md)。


2026-11-09（MPI/UDF worker猝死恢复）：FS/PG与S3/PG真实长求解中终止worker，独立Windows范围、原inode内核范围撤销和v4恢复排空通过，原停止字段与旧成果保留；60类次真实证书篡改拒绝另列为纯协议检查。新worker实际消费旧Redis消息且拒绝重放，明确新MPI/UDF/串行400单元批次、D/E和真实后处理及新求解来源引用通过，S3独立空缓存。新增4次真实DeepSeek、17222tokens，模型仅独立回读，模拟0。原内核/证书未接通失败保留。主线36/53、剩17（5进行中、12待开始）；N4-07已开始，原远端归属/跨主机停机接管及N3原条件完整保留。见[实际证据与边界](professional-worker-death-recovery.md)。



2026-11-09（同算例准备批次取消与重跑）：FS/PG和S3/PG两个真实Redis worker、MPI/UDF同时运行取消及串行排队、串行实际运行取消及MPI排队通过；所有观察出生身份退出、单次取消事件、旧不可变成果/字段、取消前指针、实际API终止后的历史/幂等和明确新三模式10000单元短求解通过，S3独立空缓存。明确修改UDF生成控制文件引起的正常失效与取消分别核验。新增4次真实DeepSeek、17132tokens，仅独立回读，模拟0；两轮验收代码失败、一轮S3观察超时及首轮清理错误保留。S3客户端观察与实际提交耗时单列，服务端预算/完整来源不变。主线36/53、剩17；剩余跟踪UDF范围、原409和原条件审计继续，N3-07未关闭。 见[same-case批次取消](same-case-batch-cancel-recovery.md)。


2026-11-09（本地准备批次）：旧源码实际复现MPI/UDF/串行三成员参数丢失、claim前invalid_request，生产BatchState改读并校验原持久spec。FS/PG及S3/PG独立投递扫描器真实停止，本地三模式400单元结果、各模式运行取消/其余不领取、原生出生身份退出、旧成果、实际API退出/幂等/明确新批次通过；完整SQL历史不变，公开本地completed到cancelled的视图差异实测单列。旧commands两个实际Python进程及用户1/批次2兼容通过，属实际OS/SQL。新增4次真实DeepSeek、17136tokens，仅独立证据回读，模拟0；三轮验收失败和旧码负向对照保留。主线36/53、剩17；N3-07进入原条件与兼容最终审计，原四条remaining保持。 见[本地参数与实际验收](local-prepared-batch-delivery.md)。


2026-11-09（N3-07原条件完成）：正式MPI/UDF桥接、查询V2来源、类型及同case批次、本地/Redis运行取消/明确恢复、实际worker退出恢复及原UDF409同类故障负/正对照已按原条件审计。31份既有实际算例与399个当前生产模块复核，原四条remaining完整保存在历史及逐条兑现映射。主线37/53完成、剩16项（4进行中、12待开始），下一项N4-05。此轮只读审计新增模型/原生执行0，复核既有62次真实DeepSeek不重复计数；模拟0。全部53/295目标及N4/N5/N6/N7原剩余范围继续。 见[原验收依据](N3-07-original-acceptance-audit.md)。
