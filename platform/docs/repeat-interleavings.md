# N5-05/G3 准备交错与实际API崩溃恢复

2026-11-09。FS/PG与S3/PG均通过真实Pandoc准备后的来源版本变化、Markdown变化拒绝，以及请求提交后、投递前实际API终止和恢复。主线N5-05按原策略条件完成；全部53/295目标继续。

两次准备交错都先完成实际原生解析，再暂停在SQL事务/算例写锁之外。另一实际HTTP请求成功修改自有来源版本或Markdown，释放后原请求分别state_conflict/input_stale，未新增Job。版本变化是明确隔离SQL故障；Markdown变化走正式文件API，不模拟原生程序或模型。

崩溃窗口中，处理请求的独立API已提交新Job/attempt、operation和投递意图；主观察API可读同一queued/unclaimed记录，尚无进程身份或parent release。核对真实Windows Python PID的父链、app/端口和进程创建时间，使用同一个原生进程句柄终止实际API，而不是只终止venv启动器。再次启动的独立API从持久意图找到同一个Job；释放前及完成后同key都返回同Job，正式worker只release一次、实际worker Pandoc compile子进程两次且全部停止核验，HTML包含当前正文。旧失败和冻结Markdown保留。

实际工程工作流等待confirm_B时，其已成功的几何producer不能另起repeat，返回state_conflict且无新Job；随后正常确认并真实完成80单元导热工程。此处不取代原N5-06工程回退和Agent checkpoint联动。

FS证据tmp/live-cli-startup-oi31rhiu；S3证据tmp/live-cli-startup-qietfjb1。各目录repeat-interleavings.json、g3-crash-recovery.json、queued见证及真实parent-release日志保留。五类普通命令的S3补验在tmp/live-cli-startup-qinb5k8w，实际20个Python作业；PDF/PPT S3补验在tmp/live-cli-startup-aoi5hnle，6个实际文档作业；几何preview S3补验在tmp/live-cli-startup-7xvfkme4。这些是原26类矩阵中缺失的后端命名证据，逐条补齐；普通命令结果不冒充CAE结果。

普通命令首轮pwlpvyin在第20个作业的提交清理阶段失败，20份封存不等于20次实际执行；该轮只有19次已执行，整轮失败、模型0保留。Windows共享句柄超过原约0.75秒删除重试窗口；现在只对32/33共享错误在3秒内重试，并继续核对目录归属及请求期限，其他错误不重试。实际CreateFileW目录句柄保留1.1秒后释放，1.313秒清理成功；持续占用3秒后仍明确失败、目录保留，解锁后可清理；无关同级文件和根目录保护不变。实际文件系统证据tmp/owned-directory-sharing-lojlaz7_/result.json。最终普通命令复验20次均实际执行并通过。原生预算和HTTP等待设置未放宽。

G3先行失败yngsa4yy为暂停夹具漏self；pjq_j5nz/kuyzy1t5为收尾busy/事件guard；bbithx8g/46tvzxsc为venv启动器PID与实际API PID不同的核对断言；jdw24_wj/gjz18yoz为测试环境没有可选psutil。改用标准库、CIM和原生句柄，不新增依赖。全部失败模型0、原始记录保留，后端保护未移除。最终请求只在进入原生暂停点之前，对明确busy保留key重试，对input_stale重新读取当前guard确认；进入暂停点后拒绝结果原样验证。

逐操作核对还补齐普通OpenFOAM与续算处理器的retry：FS tmp/live-cli-startup-b9fg66vm、S3 tmp/live-cli-startup-0fravix5。400单元原求解写出0.005并取消；冻结retry从原始输入执行，首时刻0.0005、attempt2，再次真实取消。显式resume从0.005到写出0.01后取消、attempt3；对这个续算作业retry，attempt4保持冻结检查点0.005，首执行时刻0.0055。再从原fresh处理器按当前输入rerun新根。最终retry与fresh到0.5的U/p最大绝对差均0；三次取消历史/工作流保留，同key唯一，S3移走的原运行目录不重建。两个处理器的retry分别留证。

本批七轮共14次真实api.deepseek.com/deepseek-flash HTTP、60062reported tokens，模拟0。模型真实工具回读完整场景哈希，原生/基础设施/故障证据单列，不宣称模型自主选择工程操作。15个临时PG库已删除，主API、peer及本批9个S3均退出，开发库未写入。完整要求审计见tmp/mainline-N5-repeat-policy-closure-evidence/original-requirements-audit.json。
