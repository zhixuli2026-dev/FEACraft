# 串行 OpenFOAM 检查点与显式续算

2026-11-23 起，算例可配置 `execution.restartPolicy`。默认沿用显式写出时刻。可用策略、job 和 workflow 请求值、正式记录与新验收见专项。 [串行续算策略](FF-C13-restart-policy.md)

## V24 续算兼容性

2026-11-18，在 PostgreSQL 搭配文件存储和 S3 的隔离算例中，各用 icoFoam 运行 400 个单元的 cavity 并实际取消，然后通过公开 API 请求续算。分别把网格单元数从 20×20×1 改为 24×20×1、更改初始场文件中的边界速度、把求解器从 icoFoam 改为 pisoFoam，均得到 `input_stale` 和实际发生变化的文件路径。这些拒绝没有创建新作业。缺少时刻、非正时刻、未写出的时刻、原检查点场或归档网格受损，也均被拒绝。[文件存储拒绝记录](../../tmp/live-cli-startup-kcdehj_q/resume-environment-negative.json) · [S3 拒绝记录](../../tmp/live-cli-startup-uoo83px0/resume-environment-negative.json)

恢复输入后，从 0.005 的检查点新建同一作业谱系的尝试。第一步为 0.0055，最终时刻为 0.5。原取消记录、原检查点字节和原工程控制文件均保持。两种存储上，续算与从零完整计算的最终 U 和 p 最大绝对差均为 0；S3 还在原尝试的本地缓存已移走时完成续算。[文件存储数值结果](../../tmp/live-cli-startup-kcdehj_q/solve-resume.json) · [S3 数值结果](../../tmp/live-cli-startup-uoo83px0/solve-resume.json)

首轮并行验收中，文件存储一侧等待作业 5 超时；S3 一侧记录了作业 120 秒墙钟超限，两轮都未进入模型回合。保持相同计算规模、预算和数值断言，串行复验通过。两轮通过的回读合计 4 次真实 DeepSeek 请求。[首轮文件存储](../../tmp/live-cli-startup-errwg0zi/live-result.json) · [首轮 S3](../../tmp/live-cli-startup-uh1s0px5/live-result.json) · [文件存储回读](../../tmp/live-cli-startup-kcdehj_q/live-result.json) · [S3 回读](../../tmp/live-cli-startup-uoo83px0/live-result.json)

## 历次计算验收

2026-11-08。N5-05 的串行 OpenFOAM 续算已通过 SQLite 和临时 PostgreSQL 的实际计算验收；N5-05 整项仍进行中，主线 34/53、剩 19 项。295 项全功能交付范围保留。

新增 S3/临时 PostgreSQL 整链路通过：整份原 attempt 缓存移走后，从归档检查点继续、正式发布，再 fresh rerun 比较，最终 U/p 最大绝对差 0；9 项拒绝检查、两次真实 DeepSeek 回读通过。两次作业仍使用 120 秒预算。见 [S3 缓存、分页修正与完整证据](s3-native-resume-progress.md)。这只覆盖整份缓存丢失；仅 run 或部分封存缺失、多次续算及其余既定矩阵继续保留。

## 执行方式

`POST /api/v1/jobs/{id}:repeat` 使用 `action=resume`、明确的 `source_attempt_id`、`source_version`、`parameters={"time":"0.005"}` 和 Idempotency-Key。只接受已注册的串行 OpenFOAM 源尝试，且必须是已经取消、失败或中断、停止和写入排空已核验的当前末次尝试。旧工作流尚未终结时拒绝；不自动执行、改写或推进旧工作流。

正式注册处理器 `solve.openfoam.resume` 创建新 Job 和同一根下的新 attempt，保留父尝试关系及 operation 审计；重新预约成果。`retry` 仍复制原封存输入，`rerun` 使用当前输入建立新根。对续算作业执行 rerun 会转回 `solve.openfoam` 的从零准备流程，不能隐式再用旧检查点。

新 fresh/resume worker 在调用实际求解器前，独占创建并 fsync `solve-invocation.json`，写入初始快照、实际 OpenFOAM 版本、求解器路径及 SHA、worker/runner SHA、Python 和 WSL 运行环境。`state=prepared` 只证明准备完成；取消或失败时不能凭这份回执宣称计算成功。成功证据仍需真实求解日志、输出场及正式发布校验。

续算的输入来自原封存目录和绑定该 attempt 的实际输出清单。检查整个已捕获输出是否变化，验证原输入、网格和原生回执；选定时刻必须确实写出、出现在原生日志时间范围内、为有限正值且早于原 endTime。VTK 读取所选及更早保留时刻的内部场、边界场和网格，拒绝缺失、格式损坏、非有限或不一致的场。

生成的新 controlDict 只替换明确的顶层 startFrom/startTime，原始工程控制文件保留。使用显式 startTime，而非猜测目录中的 latestTime；控制项语义依据 [OpenFOAM 官方说明](https://www.openfoam.com/documentation/user-guide/6-solving/6.1-time-and-data-inputoutput-control)。原输入和所选检查点通过同一封存流程组合，包含哈希复制、用户/节点峰值准入、持久计划及 SQL 见证；大场文件流式复制，不作为整个 bytes 对象塞进请求。检查点记录和派生控制文件是明确标记的生成输入。

原生执行前再次比对实际求解器、可执行文件哈希、OpenFOAM、Python、WSL 和 worker/runner；验证封存检查点及其校验模块指纹。运行后核验检查点未被改写、时间从选定检查点之后推进、所有正常完成证据和场数据，再发布新版本。当前性检查分别核验活动工程的原始控制和封存版本的派生控制，不把原文件覆盖成续算配置。后处理准备接受续算成果；旧场别名的冲突保留策略同普通求解。

## 实际证据

显式入口：`platform/server/live_llm_solve_resume_acceptance.py --source-profile-id 26 [--postgres]`。

| 后端 | 运行目录 | 实际结果 |
|---|---|---|
| SQLite | `tmp/live-cli-startup-iymonj_z` | 400 单元 cavity，几何/网格真实构建；icoFoam 写出 0.005 后实际取消；新尝试从 0.0055 算到 0.5 并正式发布；再从零计算作比较 |
| PostgreSQL | `tmp/live-cli-startup-u1ix4ioi` | 同一原生链；额外验证从续算作业发起 rerun 使用 fresh 处理器、新根和 number=1；临时库已删除 |

两轮最终 U、p 最大绝对差均为 **0**；比较的是两次真实求解的 VTK 场数组，检查有限值及 400 单元。各 7 项拒绝检查：非正时刻、未写出时刻、缺少时刻，以及改动检查点场、网格、初始场和调用回执。原取消 Job/attempt 和工作流均保留取消状态；新 attempt 的封存、父尝试、发布、幂等重放及原始工程控制保留均通过。详见各目录 `solve-resume.json`。

原生、基础设施和协议断言均直接读取实际进程、HTTP、文件与数据库结果；每轮结束后另由真实 DeepSeek Agent 选择 read_text_file 回读证据摘要。**4 次真实 api.deepseek.com / deepseek-flash HTTP、17,306 reported tokens，mock 0**。不把模型回读算作模型驱动了求解决策。上游回执见 `upstream.jsonl`，主 API 均已退出。

失败 `tmp/live-cli-startup-hqg5p9p6` 保留：真实取消及前置检查后，生成控制文件时将 startFrom 的值 startTime 当作另一个键，拒绝续算；0 模型调用。已修正为跳过配置值，使用新隔离算例取得上述两轮成功。未放宽校验或模拟求解/模型结果。

测试导入路径已审查：cae_fixtures 提供纯输入数据，casedict 提供文本编辑；live_llm_job_repeat_acceptance 复用实际请求和临时库执行器，live_llm_cli_startup_acceptance 明确传入本场景。早期运行的 fixture 为空，当前为只转发实际调用的计时、源码哈希和状态采集夹具；原生过程未注入替代推理结果，也未运行历史模拟回归集合。

## 尚未验收的范围

- S3 已归档输出独立恢复和整份源 attempt 缓存丢失已通过；仅 run 目录丢失、部分封存缓存缺失仍需补齐策略和验收。不能把恢复的初始 run 目录冒充执行结果；缺乏完整可信现场时继续拒绝。
- 再次取消续算后的多次续算/重试、崩溃恢复，以及真实工具链更换、二阶/变步长历史、多区域和其他求解器的完整适用矩阵。
- 其余注册处理器的重试/重跑、旧 argv 的明确准备方式和专业依赖变化；工作流、阶段确认及 Agent checkpoint 联动继续 N5-06。

本批实际覆盖 icoFoam、单区域 2D cavity、Euler 固定时间步。它证明这条续算主线可用，不证明所有 OpenFOAM 求解器、MPI、SU2、CalculiX 都可 resume。原 N5-05 和全功能完整范围不缩减。
