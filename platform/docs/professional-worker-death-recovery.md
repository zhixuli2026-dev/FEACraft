# 专业作业 worker 猝死恢复

2026-11-16 增量：完整准备后、running 登记前的 Windows Pandoc 和 WSL MPI 子执行失联已实际补齐，分别见 [报告子执行](child-startup-loss-recovery.md)和 [WSL 子执行](wsl-child-startup-loss-recovery.md)。当前主线为 48/53；下文 36/53 等计数及初始缺口描述为 2026-11-09 历史证据，不能代替当前台账。更早准备窗口、其他取消/外部 owner 和完整故障矩阵继续保留。

主线仍为36/53、剩17项，N3-07的原四条remaining及全部295项范围保留。MPI/UDF worker猝死、内核范围、v4排空及明确三模式新批次已通过FS/PG与S3/PG命名场景。下列初始缺口和失败保留，不把初始Windows范围证明单独写成完整排空成功。

## 当前通过范围

FS/PG证据 `tmp/live-cli-startup-va_vakb3`：真实长MPI/UDF运行期间终止两个原worker，独立Windows凭据和原boot/device/父inode/范围inode的Linux撤销回执由新API核验。两个gate均保持原UID65534及原映射，旧成果与场数据保留。v4证书的`state=recovered_stopped`明确区别于原worker停止记录；原root/child停止字段仍为空，原child运行历史不修改，证书保留原输出、事件与快照摘要、核验者身份。两份实际证书各15类篡改被拒绝，属于纯协议检查。

新启动两个真实arq worker，实际消费重新投递的旧Job 20和22，原投递函数均返回`already_owned_or_terminal`，没有新尝试或求解。随后明确新批次的MPI Job35、UDF Job40、串行 Job39各完成400单元求解、D/E及实际后处理；场和平均压力保持原阈值，旧批次/流程历史与新旧幂等键保留。新增2次真实DeepSeek独立证据回读，模拟0。故障与新worker退出、PG删除、Redis停止和观察到的原生进程清理均有回执。其余模式的worker故障及广泛故障矩阵仍按原条件继续。

前一轮 `tmp/live-cli-startup-i2rsz44s` 已实际完成分配前准备、分配、非root入组与两范围撤销，但排空证书当时未接通，整体仍失败、模型0；不得将这一轮改写为完整通过。

## 已取得的实际证据

`live_llm_professional_worker_death_acceptance.py` 先完成正式MPI/UDF和实际后处理成功基线，再建立10000单元长求解批次。实际观察非root MPI ranks、载入本次库的UDF icoFoam及strace后，核对两个arq worker的真实出生身份、完整启动器祖先链及脚本路径；在同一个打开的Windows进程句柄上终止它们。没有按进程名或未绑定PID执行清理。

原反例 `tmp/live-cli-startup-e5aj21oi`：两个真实worker及所有观察到的原生出生身份退出，原成果保持。新API将作业标为interrupted，但`workflow-job-drain`仍为false，无法安全进行原算例重跑。模型调用0，临时PG删除、自建Redis停止及故障清理均有回执。故障清理只针对原观察出生身份使用pidfd，不补造数据库停止记录。

## Windows范围的独立凭据

现将已有Windows Job独立守护器接入注册工程作业的根进程和原生子进程。守护器在第一条业务进程创建前接收实际空Job句柄，绑定原worker、nonce、目录inode和实现摘要。原worker退出后，守护器仍保留句柄和成员观察，封闭同一Job、等待原始成员退出并保存不可替换的凭据。每个守护器在接收Job句柄前取得以其实际出生身份持有的节点容量预留；worker退出不会回收尚可能写入的凭据容量。凭据目录位于节点运行元数据中，不塞入公开工程成果。

`tmp/live-cli-startup-ucg9n4bg`：成功基线继续通过，随后两个真实worker被终止；四个独立范围均保留完整凭据，累计24个Windows成员退出且无未决成员。恢复API按原Job/attempt/父子launch及worker归属验证并记录四项`attempt.scope_guardian_observed`。原始进程/子进程停止字段没有被改写，`native_tree_verified=false`、`drain_certificate_created=false`，整体故障验收仍失败。真实模型0，清理错误为空，PG删除、Redis停止。

守护器最长寿命支持原工程作业24小时上限及30秒清理余量；CLI原360秒选择继续由其自己的配置约束。Windows Job终止与对象生命周期的依据为微软[Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)。实现仍以原句柄和完整成员凭据为准，不把名字查询失败或活动计数直接当作完整停止证明。

## 初始完整验收条件

1. 为原生子进程复用已验证的内核cgroup控制器，持久化分配前的创建者/预留、分配结果和原child/parent归属，覆盖创建完成但回执未提交的窗口。
2. 子gate先进入绑定的内核范围，再降至原非root身份，之后才发送准备帧和接受运行；保持MPI/UDF原身份映射、工具链和输入策略。
3. 恢复必须先证明原worker与Windows范围已退出，再以原boot/device/父inode/范围inode撤销原Linux范围，并核验原gate身份。未知或缺少准备历史继续要求人工处理。
4. 增加有独立schema和来源审计的恢复排空证书；保留原来缺失的停止事实，不伪称原worker曾写出它没有提交的凭据。保持声明、取消、成果指针、旧历史与幂等边界。
5. 完成原算例明确重跑的MPI/UDF实际场和后处理；真实FS/PG及S3/PG（独立空缓存）验证，并核验重新启动worker不重放旧作业。整体通过之后才允许称为worker猝死恢复完成。

上述下一段继续原主线条件，不添加或替换53项任务。准备运行的同case批次取消、其余跟踪范围、原S3 UDF单次409及后续全部主线/全功能交付继续有效。

## 内核接线的实现顺序

复用 `native_scope_control.control(..., 'allocate', {'seed': ...}, on_prepared=...)`；回调先保存实际创建者、原父目录与缺席预留，随后才允许建立范围，结果再保存完整scope inode。分配返回的nonce由种子和实际创建者决定，因此必须在 `child_launch_registration.reserve` 及Windows守护器准备之前，更新该child的OwnedProcess nonce。原child预留已存在，新的分配事件继续绑定同一child和parent，不能伪造CLI会话来复用CLI专用表。

原生命令继续使用原程序和参数；只有固定外层gate以root身份进入已绑定cgroup，随后在发送身份帧和任何用户程序运行之前降至原nobody身份。固定gate参数必须要求有效scope，不能提供任意UID或任意命令控制口；保留原非root映射验收。正常结束先停止原Windows/原生gate、再撤销绑定内核范围，异常准备阶段使用原分配准备记录执行abandon，不按名字猜测范围。

恢复只在原worker出生身份退出且四项Windows范围凭据核验后进行。对已完整注册的native gate使用原inode范围撤销回执及gate退出核验；缺失准备/入组历史的部分启动继续保留requires_attention。恢复证书使用独立版本，记录原停止字段缺失、原事件/快照摘要及当前核验者，不伪造root退出码、控制帧或原worker停止事件。最初入口在排空通过后还有明确新批次矩阵待实现的失败保护；目前已替换为真实旧消息投递及明确三模式新批次，范围见当前通过记录。

当前v4证书只接受已封存的注册私有执行、Windows根进程、完整子gate/守护历史和原收尾输出为空的interrupted尝试。直接原生根、缺失准备、已有finalizing输出等情况继续使用各自原恢复协议或requires_attention，不用本证书绕过。文件存储版进一步通过不可变后处理manifest复核三个新求解版本的真实来源引用，见该运行的`worker-death-postprocess-source-links.json`。本项未覆盖的更广故障条件继续验收。

## 双后端归档

S3通过运行 tmp/live-cli-startup-ki76rgin，原字段、实际旧消息拒绝、新三模式结果及后处理来源引用保持。3个临时PG删除、1个自建S3停止、3个Redis停止、16个worker出生身份退出及18个API端口无残留核验；前两轮Windows基础失败见上一归档，本轮内核已撤销但证书未接通的失败继续保留。源码、全部Python AST及原remaining对照见 tmp/mainline-N3-07-worker-scope-recovery-evidence/validation.json。

2026-11-09（MPI/UDF worker猝死恢复）：FS/PG与S3/PG真实长求解中终止worker，独立Windows范围、原inode内核范围撤销和v4恢复排空通过，原停止字段与旧成果保留；60类次真实证书篡改拒绝另列为纯协议检查。新worker实际消费旧Redis消息且拒绝重放，明确新MPI/UDF/串行400单元批次、D/E和真实后处理及新求解来源引用通过，S3独立空缓存。新增4次真实DeepSeek、17222tokens，模型仅独立回读，模拟0。原内核/证书未接通失败保留。主线36/53、剩17（5进行中、12待开始）；N4-07已开始，原远端归属/跨主机停机接管及N3原条件完整保留。见[实际证据与边界](professional-worker-death-recovery.md)。

