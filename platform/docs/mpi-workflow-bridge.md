# 正式MPI工作流桥接

2026-11-09，N3-07的MPI联动子阶段通过FS/PG与S3/PG实际验收。N3-07仍in_progress：原生运行中取消/恢复、UDF、完整多工况工作流和原S3 UDF单次409归因继续；本批不是整项关闭。主线36/53、剩17，4进行中13待开始，全部295项目标保持。

## 实现与接口

创建、重算及旧计划显式迁移的parameters新增可选solve_mode（serial/mpi）及subdomains（1～64整数）。未传新参数的旧图保持原样；MPI默认2子域，串行模式不能附带分区数。MPI预算使用正整秒，不把用户预算静默延长。示例：parameters={"geometry_mode":"export","solve_mode":"mpi","subdomains":2}。

solve.openfoam.mpi重用原有MPI worker/native/artifacts和受控执行器，固定当前已确认网格的不可变版本。注册网格与工作副本不能重复挂载相同路径；正式桥接现在只从固定发布挂载网格，其余文件和空目录独立封存。原生输入、PMIx配置、非root、ranks及分解/求解/重构仍由原验证器核验，没有改写求解器。

结果使用正式solve通道及workflow-mpi.v1描述符，内嵌原始MPI描述符和真实receipt；不伪造串行solve-evidence。D确认引用mpi-evidence.json，下游后处理固定该MPI发布与网格来源。生成的decomposeParDict保留在私有尝试，避免改动网格来源。复制到工作副本的产物使用原始输入摘要作为CAS基线；旧串行专有产物按签名退休计划删除，外部修改仍保留，损坏历史bundle的受管路径回退也识别MPI。

依赖观察记录subdomains；重算规划既核对处理器，也核对分区数，不因当前输出仍fresh而复用不同分区的求解。原v1/v2串行图在5个目标、A–E起始阶段和原参数组合共250项与N5-06归档逐项一致；这是纯计划协议检查，不当作250次原生或模型运行。

后处理prepare原本物化整个算例；现在与source检查共用按配置、初始/常量/控制文件和外部引用选择的短时输入视图。读者仍重新解析字典并校验实际引用；case-file后处理变体继续读取当前run。真正的结果场由固定求解发布挂载，未减少原生场、指标、readiness或引用哈希校验。

## 真实验收

每种后端先运行400单元串行icoFoam至0.5并明确确认D，再显式重算到MPI/后处理目标：复用原几何与网格，B/C仍须分别确认；实际2个非root rank完成分解/求解/重构，结果在solve通道发布。速度相对串行最大误差1.0000000000287557e-6，小于原1e-5标准，未放宽误差。

工作副本控制文件未被生成的分区配置覆盖，mesh/solve保持fresh；D未确认时不能推进。实际核验停止/排空后终止原API，新API查询相同运行、步骤及Job清单，不重跑已完成MPI；明确D后才执行固定来源的后处理。压强体平均值0.022259352333744136，单位m2/s2，readiness通过后明确E完成。原串行运行历史不变，相同key回到原新运行。

再将subdomains改为4，规划要求重新执行solve、复用原几何/网格；在确认前取消后没有新原生尝试。本检查不声称本轮实际完成4-rank求解，也不替代运行中的MPI取消/恢复验收。

| 组合 | 最终场景 | 真实模型调用 |
|---|---|---:|
| FS + PostgreSQL | tmp/live-cli-startup-t8ah2gz7/mpi-workflow.json | 2 |
| S3 + PostgreSQL | tmp/live-cli-startup-zy2jsl78/mpi-workflow.json | 2 |

另有物化优化前的完整FS通过：tmp/live-cli-startup-u79p4elo。合计6次真实api.deepseek.com / deepseek-flash HTTP200、25682tokens，模拟0，全部有最终usage。模型调用均为独立文件摘要工具回读，不宣称模型自主规划了MPI。工程执行、API终止和计划协议证据分别保存。

最终带请求观察的FS最慢5.516秒，S3最慢27.844秒；这是各自这一轮的样本，不能称完整性能SLA。HTTP客户端仍30秒，没有因验收放宽。S3先行超时的准确请求位置未留观测，后续通过不能替它补造原因；保留原失败及性能风险，继续N7-04。

## 保留的失败与下一步

8nydxhun：MPI未创建Job，ready步骤等待超时，模型0；后加派发回执检查。g1hm0i4y：确认C后的派发明确返回网格重复挂载，模型0；已修正。fjwb4d5p：实际MPI及重启/下游提取运行通过，但未配置提取项，E readiness正确拒绝。kwysx70i：实际压强平均已得到，单位未声明导致readiness拒绝；补真实m2/s2声明，不放宽门控。shstlmmw：S3实际MPI及重启通过，后续HTTP30秒超时，原失败保留。五轮均模型0，没有回退到模拟。

8个临时PG库已删除，2个自建S3服务已停止；只读进程复核无本批API/驱动残留，开发库仅只读获取授权profile26凭据。新入口及所用辅助、私有观察代码已审查；未运行历史模拟回归。核心MPI/UDF原生文件、执行器与投递/停止服务与上一归档相同。

从platform/server使用.venv/Scripts/python.exe -X utf8 -B live_llm_mpi_workflow_acceptance.py --source-profile-id 26 --postgres，可加--s3。源码归档platform/backups/mainline-N3-07-mpi-workflow-20261109.zip，SHA、AST、失败和组合证据见tmp/mainline-N3-07-mpi-workflow-evidence/validation.json。

后续严格按[N3-07原条件清单](N3-07-closure-plan.md)：实际运行中MPI取消/明确恢复；共享多来源输入选择须保留目录粒度并验证超过256文件，当前内部文件展开不作大型算例准入承诺；UDF构建/库/集成的正式工作流与取消恢复；多工况正式模式、配额/归属/批量取消；原S3 UDF单次409仍未归因。所有原remaining保留，不把本轮MPI命名场景或一次S3通过视为整项/全功能完成。
