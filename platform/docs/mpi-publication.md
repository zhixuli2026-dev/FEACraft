# MPI独立运行与重构结果发布

## 2026-11-22 旧分区与状态读取

`POST /cases/{id}/mpi:run` 接受可选布尔 `force`。省略时沿用原行为。显式 `false` 在存在直接 `processor<N>` 目录时返回 `409 mpi_processor_exists`，干净输入照常分解。显式 `true` 在本次私有工作区使用 `decomposePar -force`，通过原有验证和发布流程更新兼容文件。分解选项与输入一起封存，完成后核验实际启动参数。

`GET /cases/{id}/mpi:status` 读取文件清单及所有权水位，按编号列出直接分区目录，按数值列出非零时间。任一时间缺少重构 `U` 时报告未完成。四组 SQLite／PostgreSQL 和文件系统／认证 SeaweedFS 实测负时间、科学计数法、顺序、嵌套目录排除和跨账号拒绝。认证 SeaweedFS 读状态时未下载实际 40 MiB 工程文件。 [状态实测](../../tmp/ff-c06b-mpi-status-s3-d7ak8g63/mpi-status.json) · [本地分解选项实测](../../tmp/ff-c06b-mpi-force-wm2wl2e5/mpi-force.json)

SQLite 文件系统和 PostgreSQL 认证 SeaweedFS 分别真实完成两次 `icoFoam` 两 rank 求解。首次干净分解的回执没有 `-force`，原键重放仍是同一作业。后一请求显式禁止强制分解，返回 409，原 `processor0` 网格字节保持。明确重新分解后，实际命令包含 `-force`，提交独立发布，两个分区和重构状态可回读。远端早期轮次保留账户暂忙、客户端超时和夹具断言，最终使用更小的真实算例经过同一公开流程。全部隔离 API、认证 SeaweedFS 和临时库已收尾。 [两组原生分解与状态合并回执](../../tmp/FF-C06B-mpi-status-20261122/validation.json)

FF-C06B 按 T04 完成。既有真实串并行数值、临时分解、另一个 API 取消和原输入重试仍可核对，Redis 调度与 Linux worker 的测试部署证据已留档。本机跨进程验收沿用原证据，下一台物理宿主属于 N7-01。此次状态和分解专项没有模型调用，历史真实模型回读保留在原验收。 [串并行及工作流](mpi-workflow-bridge.md) · [跨 API 取消](N7-01-mpi-cancel-events.md) · [原输入重试](mpi-repeat.md) · [Linux worker 实测](linux-mpi-udf-deployment.md)

## 独立运行初版

本批本地文件存储的 `/mpi:run` 已接入 `mpi.openfoam@1`，完整用户文件封存到独立尝试目录。固定Windows worker通过所属原生通道启动WSL中的非root MPI；主工作副本只在结果验证和发布提交后更新。S3仍走原入口，本批不宣称已完成S3迁移。

## 运行和结果

配置绑定求解器、进程数、运行目录、WSL发行版、OpenFOAM环境、输入摘要、资源限制与执行标识。既有decomposeParDict保持原样，由实际foamDictionary读取numberOfSubdomains并与请求核对；缺少字典时只在独立运行区生成。decomposePar的force仅作用于本次Linux副本，用户processor目录不在运行前删除。

原生阶段依次分解、mpirun求解、reconstructPar重构，记录各自argv、日志摘要、工具路径/摘要、OpenFOAM版本和非零UID。绝对路径mpirun使用`--noprefix`：避免OpenMPI隐式设置安装前缀后改变rank的库搜索顺序，误载其他OpenFOAM库。实机OpenMPI4.1.6帮助与实际串并行比较验证了该修复。

并发验收曾出现`mca_btl_vader_poll_handle_frag`段错误和另一作业不完成。两个私有PID命名空间仍共享默认MPI通信临时文件；当前显式设置`orte_tmpdir_base`与`btl_vader_backing_directory`，将会话和共享内存文件放在该次UUID目录的`.vibeflow`下，排除于科学产物清单。该碰撞原因是结合本地故障和进程隔离方式的判断，最终以修复后的并发复验为准。参数经本机4.1.6的`ompi_info`核对；共享内存文件机制参考[OpenMPI 4.1.6源码](https://raw.githubusercontent.com/open-mpi/ompi/v4.1.6/opal/mca/btl/vader/btl_vader_component.c)与[官方共享内存说明](https://www.open-mpi.org/faq/?category=sm)。

含空白或特殊字符的运行目录仅在Linux副本中逐级映射，保持目录深度与相对依赖关系，回传时恢复原路径。OpenFOAM续算产生的内部uniform链接按普通文件字节读取并回传；越出作业目录、循环或非普通文件拒绝，扫描限制为10,000文件和1GiB。该限制不是完整内存/CPU/输出写入配额。

重构后重新检查输入未被修改、processor集合、三阶段成功记录、日志最后时间和实际输出时间；通过VTK读取每个结果时间的内部与边界场，检查字段分量、网格和有限数值。保存独立MPI证据与收据，新写入/改写的文件和删除清单构成不可变成果。来源或目标文件被用户修改时，拒绝覆盖。

发布通道为`mpi:`加运行路径摘要，后续作业可引用当前或历史版本。旧算例入口没有已确认网格版本的完整工作流谱系，因此不把它冒充主线`solve`确认门的成果；N3-07的正式MPI/UDF工作流桥接仍待完成。

## 验证和限制

`mpi_publication_smoke.py --postgres`验证真实串并行U场比较、用户指定scotch分区、重复执行、latestTime续算、含空格路径、参数不一致、配置/收据破坏、来源/目标冲突、取消和发布中API退出后的恢复。最终双库各14项发布检查、双库各6项原生中断/身份检查通过，两套真实MPI验收同时运行。规范证据：`tmp/mainline-N2-mpi-publication-evidence/evidence-origin.json`。

`mpi_scope_smoke.py`改为检查真实`mpi_run`子执行的进程身份，保留两个icoFoam rank、非零UID、私有namespace、取消/超时/日志限制、错误UID/发行版拒绝、API退出和外部哨兵存活断言。新增输入复制和子执行握手需要等待真实rank出现，观察窗口调整为35秒，超时用例墙钟上限45秒。

正常退出及能处理SIGTERM时，原生helper清理其UUID对应的Linux目录，保存路径、设备、inode与removed记录。SIGKILL不能执行finally；不能仅凭API退出宣称暂存已清理。强杀遗留目录恢复、缺少物理停止证明的恢复、全部进程数和物理模型矩阵、大数据/并发、S3、跨主机调度与正式工作流桥接继续保留在原任务中。

本次实际核查8份中断暂存记录，其中API退出的两例留下目录；其余6例目录已不存在。确认捕获的原生进程全部退出，再按路径、UID、设备和inode复核后，仅清理了这两个测试目录。原始removed记录未篡改；测试清理不等于产品自动恢复实现。审计见`tmp/mpi-native-staging-result.json`与`tmp/mpi-test-stage-cleanup-result.json`。

本批不关闭N2-03、N3-07或FF-C06B整项。主线仍20完成、14进行中、19待开始，剩33/53；295项完整交付要求不缩减。Redis沿用已通过真实队列重建验收的独立WSL方案，用户已授权自行选择Docker或恢复方案，无需再次询问。
