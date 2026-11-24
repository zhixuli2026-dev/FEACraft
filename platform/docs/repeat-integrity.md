# N5-05/G2 来源、版本、工具链与归属拒绝验收

2026-11-08，G2命名检查完成；N5-05仍等G3收口。主线34/53、剩19项，53及295项目标保持。

## 实际发现及修复

提交记录原先未核对case归属；正式成果预留失去handler记录时可能进入旧专业命令兼容分支。现在原Job/attempt/提交case必须一致，已有正式成果预留缺handler明确evidence_invalid。新尝试在创建时绑定提交请求摘要，重试也绑定新操作请求摘要；原请求摘要变化拒绝。旧无摘要字段的历史不补造摘要，沿用原有封存、处理器与归属检查；兼容分支另有协议检查，不等同于证明损坏历史可执行。

FS和S3各从两次真实CalculiX梁求解开始，实际生成FRD后遇独占receipt冲突失败。对真实隔离库记录依次注入23类损坏：未知/错误/缺失handler或版本、执行hash/spec、提交spec/hash/绑定/case、Job类型/命令、封存attempt/case/Job、成果缺失/attempt/case/kind/schema/descriptor，以及声明工具链摘要。公开操作不可用，实际repeat返回对应evidence_invalid/recovery_required；逐次恢复，并检查没有新增Job。这些是明确SQL故障，原生求解和真实模型证据分开。

健康控制使用原封存重试并正式发布；原失败和冻结文件不变。同键重放仍返回同Job；已退位源尝试用新key重试拒绝。实际修改include后拒绝retry。CalculiX resume明确checkpoint_required。

## 实际工具和上游变化

在自有目录复制实际ccx_static.exe，初始SHA与安装原件相等。给自有副本增加PE overlay后，旧声明的retry可分配新attempt，但worker在启动CalculiX子程序前校验失败；实际child journal为空，没有solver.log或FRD。显式当前输入rerun记录新SHA，真正执行更新后的副本、正式发布；8节点位移与原真实结果一致，旧不可变成果保留。原安装文件未改。这里的拒绝发生在原生启动前，不能误称HTTP准入拒绝。

独立80单元导热工程中，真实后处理失败后实际新根生成另一份网格发布；旧postprocess retry因原上游不再当前而input_stale，未新增Job，旧失败保留。该80单元专用网格沿用10×4×2，原N7-03三基准入口继续按原条件验收。

## 检查点、运行时和公开策略

两端实际400单元icoFoam写出0.005后取消，核验停止；配置切换到另一允许发行版时resume返回runtime_unavailable，当前controlDict改为另一solver时返回input_stale。恢复原配置后实际续算到0.5并正式发布，再从零计算比较，U/p最大绝对差均0。原已取消历史、原控制和检查点保留。FS共10类、S3共12类负例；S3还核验归档缺失/损坏并移走完整源缓存后从独立归档视图续算。

从真实原生调用回执对8项toolchain字段各做缺失/变更验证，属于纯协议字段故障，不宣称实际更换了全部OS/Python。26类已注册处理器的104项合成状态检查与公开recovery声明一致，只有solve.openfoam和solve.openfoam.resume可选择resume；其他处理器明确checkpoint_required。这部分不冒充26类原生求解器已支持续算。

已有缓存细分及重复取消证据继续有效：见[独立归档输入](archived-input-recovery.md)、[已有续算](openfoam-checkpoint-resume.md)、[S3证据](s3-native-resume-progress.md)；各处理器实际失败/重试/重跑见[26类矩阵](job-repeat-handler-acceptance.json)。本批不重跑历史模拟集合，也不把旧模拟记录当真实模型验收。原要求映射见tmp/mainline-N5-repeat-integrity-evidence/requirement-evidence-map.json。

## 证据和边界

完整性FS tmp/live-cli-startup-xlt9cjow、S3 tmp/live-cli-startup-c4k5hm9z；环境/续算FS tmp/live-cli-startup-v98xdteu、S3 tmp/live-cli-startup-jf8wkrjo。四轮共8次真实api.deepseek.com/deepseek-flash HTTP、34246reported tokens，模拟0。模型只通过真实工具读取报告哈希；实际工程与故障操作由测试直接发起。5个临时PG库已删除、API及本批2个S3均退出，开发库未写入。

首轮c8wm1woj已完成23类拒绝和工具变化拒绝，但当前重跑确认遇收尾事件序号变化而input_stale；整轮失败、模型0保留。后续测试显式读取新guard重新确认，busy保留原key，input_stale后使用新确认和新key；后端保护未移除。真实完整两轮结果独立记录。

G1已归档，G2完成上述命名要求；原G3原生准备交错与提交后崩溃仍须完整证据后关闭，N5-05尚不计完成。完整工作流/审批/Agent checkpoint继续原N5-06，完整跨环境/负载及295项最终交付继续原计划。
