# N6-03 原API条件核对

2026-11-11。按`next-stage-mainline-plan.md`原表，N6-03是“实现工作流创建/列表/详情/取消/恢复API，完善幂等与归属校验”；原验收句为“只在有权case下操作；外部幂等键不能跨用户复用；跨用户资源按既有约定拒绝”。本页按此API范围关闭N6-03，主线40/53、剩13（2进行中、11待开始）。全部295项目标和原N4/N6界面/N7任务继续，目标未完成。

## 条件、实现与实际证据

| 原条件 | 当前实现与证据 |
|---|---|
| 创建 | 注册计划、固定参数、预算、case修订和持久提交后派发；`workflow_execution.start`/`workflow_scheduler.create_run`；真实几何、串行/MPI/UDF、报告等既有注册入口保留。查询阶段SQLite/PG实际创建及Actor键检查见`workflow-api-queries.md`。 |
| 列表与详情 | 保留原最近50条数组接口，新增case/筛选绑定分页、稳定同时间排序；详情严格检查计划、步骤/attempt/Job归属及结构。SQLite/PG各5组包括实际创建、只读权限、跨case/损坏JSON明确拒绝，见查询阶段validation。 |
| 取消 | expected_version、持久取消与原owned进程控制；取消收尾保持cancelling至原归档/排空完成。真实CadQuery、串行、MPI、UDF均有取消和有效根/子进程停止证明；S3归档取消路径实际修复并验证。 |
| 恢复与决策 | `resume-plan`、`:resume`、`recovery-request(action=resume)`、v5来源/新图/请求族绑定；区分原输入重试、支持的串行检查点续算、显式当前输入重算及已知旧v1迁移。未知停止/封存/处理器不降级执行，MPI/UDF检查点方式明确拒绝。原子重试、四组合连续串行检查点、实际MPI/UDF重试均见`workflow-resume-atomic.md`。 |
| 幂等 | case+actor+key+完整参数；锁前及等待控制锁后查原回执，提交前再次查回执，原请求族head CAS。真实双请求同键一份执行、异内容拒绝、退役head拒绝、同确认精确回读已有证据；既有双API原子claim保持。 |
| 权限与归属 | 每个新读写入口先按case重新授权，写回执重放也检查写权限；游标绑定case，执行/来源/步骤/attempt/Job/编译发布逐层绑定。真实跨账户隐藏、viewer可读不可写、不同actor同外部键不共享Run，以及损坏跨case关联拒绝通过。 |
| 前置 | 正式台账N1-07、N5-07均已complete；N3-06文档/LaTeX持久API和N3-07专业桥接亦已complete。当前恢复不替代这些原生/版本证据。 |

API生产实现主要在`app/routers/workflows.py`、`workflow_execution.py`、`workflow_queries.py`、`workflow_resume.py`及原recompute/migration服务。本段新恢复图的Job/attempt、输入、发布预留、审计和交付意图同事务提交，后续才执行；未先调用会自行提交的Job repeat API再补步骤。

## 原remaining文字保持及归属

原台账两条remaining全文在关闭记录中保存，不删历史：

1. “HTML/DOCX、compose与LaTeX持久版本API已接通；完整版本UI、历史源码阅读、恢复/事件与专业目标继续实施”
2. “resume与恢复决策、事件查询、其他专业目标及前置N1-07/N5-07仍待完成”

其中API、事件、恢复/来源、注册专业模式及前置已对齐上述实现与实际证据。完整版本UI、历史源码阅读、页面选中尝试/版本和整合流程仍按原N6-04～07继续；这些工作没有在本页声明完成，也不因API关闭而从53/295目标移除。原计划的JobsPanel、CasePanel、刷新恢复和完整浏览器流程分别有独立任务，不能把它们的后续交付等同于API验收。

## 验收边界与未决故障

本次专业验收最终4组目标场景：UDF＋FS/SQLite、UDF＋S3/PG，MPI＋FS/PG、MPI＋S3/SQLite；每组2项、共22个实际Job，6个被观察的icoFoam进程出生身份均确认退出。两组UDF均实际wmake/CDLL、原ELF复用、仅新增一个求解Job、无重复编译及损坏投影拒绝；MPI两rank、1600单元，UDF400单元，最终U/p有限非零并实际D确认成功。不是全部环境和故障的笛卡尔矩阵，原N7继续。

原串行四组合、两次检查点、S3缺失本机结果和fresh数值对照保持；本轮仅修改UDF前缀引用分支，串行/普通重试生产路径保持。没有运行历史混合模拟回归，本段模型0、模拟LLM0。

首轮MPI在100输出时刻配置下的reconstructPar以SIGKILL退出，直接原因未证明；不能据后续10输出时刻成功称为修复。原失败、原原生/接口退出与PG删除证据保留，诊断由原N4-05资源失败分类、N7-04性能/原生边界继续。本段专业验证另有identity尚未登记时的验收空值错误，明确记录为脚本返工；不计为产品通过。独立API/跨主机、完整源/head/取消/交付提交故障矩阵仍归原N4/N7，不在此冒充全部完成。

接口阶段退出不代表整个产品或全部295功能交付，后续开始原N6-04界面整合。


2026-11-12后续：上述重叠备忘中的完整版本UI、历史源码及刷新选择现由原N6-06按原条件完成，见[N6-06退出审计](N6-06-original-closure-audit.md)。完整浏览器入口合并与全功能/部署矩阵仍继续N6-07/N7/295。
