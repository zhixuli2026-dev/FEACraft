# N6-07运行中作业重启恢复

2026-11-12。承接[Agent与空闲服务重启增量](N6-07-agent-restart-and-permissions.md)。本次真实模型0、模拟LLM0，测试对象为实际API、Windows/WSL进程、浏览器及持久恢复协议；前一增量4次真实DeepSeek证据继续单列。

## 反例

隔离宿主`tmp/case-workflow-controls-nu1hl1p_`通过375px页面提交真实Windows命令：输出开始标记、ping等待120秒、输出结束标记。首次提交409的原因未捕获，保留该观察；页面沿用相同键重试后创建作业28。原日志只有开始标记、实际进程身份已登记且phase=running时，停止API并在同一环境/存储重启。

重启正确标记原尝试interrupted，页面刷新无POST，没有自动重跑。但原作业未登记独立范围见证，恢复证书为空，所有重试入口不可用。实际根进程出生身份已退出只是窄观察，不能补成完整停止证明。原始`controls-result.json`保持passed=false、cleanup_failure为停止证明观察超时，API/preview实际退出；旧业务历史和源数据未变。

## 修复

`job_scope_guardians.prepare`现在也为已封存输入、无专业处理器/发布预留/工作流绑定、使用Windows普通shell执行的任务，在启动前登记既有独立Windows Job范围见证。恢复端允许此明确的执行类型使用既有v4证书，仍逐项验证原worker退出、独立guardian退出、原范围身份及完整范围关闭回执。原stop字段仍为null，不伪称原worker执行过回调。

旧无封存命令、带argv的处理器、遗留求解目录、直接WSL根、工作流或发布预留不能进入新增分支。直接WSL/sandbox需要独立Linux范围协议；Windows Job见证不能替代Linux树停止证明。本增量未完成该范围，后续继续。

测试宿主新增明确的运行中重启请求：只能选择本轮新建、唯一active且已有真实running身份的作业；旧历史行必须保持。读取恢复证明的测试接口调用生产证书校验器，不写业务数据，不构造证明。

## 最终源码实际验收

宿主`tmp/case-workflow-controls-brsnox72`中的作业28实际运行后API被停止并重启。原作业保持interrupted，原process_stop=null，新增的独立范围证书为`vibeflow.job-write-checkpoint.v4/recovered_stopped`，有1个Windows范围见证且通过生产校验。原日志仍只有开始标记，刷新没有POST重放。

页面加载恢复方案时按钮曾处于loading禁用态，该原始观察保留；等待实际方案加载完成后，“重试原输入”可用。核对期间没有新作业；点击“确认并创建新尝试”返回202，创建作业29、attempt number=2、root_job_id=28，retry_of绑定原attempt。新命令实际输出开始标记后，在页面明确取消，最终cancelled且原停止回执verified=true。再次冷刷新保留作业29，无POST，375px默认日志高度351.42px。

宿主最终退出0，原作业28按独立恢复证书核验范围排空，新作业29按原worker停止回执核验，两类证据分别保存。API/preview退出、原流程/步骤/作业/尝试完整行未变、源数据库/索引未改均通过。审查绑定993份源码/构建文件：`tmp/N6-active-job-restart-review-v2-20261112.json`。

`tmp/job-starting-cancel-9lzmc_gl/starting-cancel-result.json`是最终源码实际取消回归：WSL批次和Windows单作业在created登记后取消，载荷未执行，真实停止与排空完整，同算例后续真实命令成功；排队成员未领取。此前测试脚本错误账户清理的问题已用当前脚本实际复验，整个宿主passed=true，API退出已核验。旧`z75sz201`失败原件没有被改写。

`tmp/N6-Windows-shell-scope-certificate-audit-20261112.json`只读校验实际v4证明，并对内存副本验证13项拒绝边界：缺范围、未核验范围、原owner仍存活、伪造原stop、错作业、未封存、直接WSL、argv、旧求解目录、改命令、工作流绑定、发布预留，以及旧失败借用新证书。没有修改SQL或模拟推理。原`nu1hl1p_`的空证明仍不能恢复。

## 未完成范围

主线仍44/53、剩9；全功能295项原目标继续有效，整项最终验收仍0。继续直接WSL/sandbox运行中中断的原生范围恢复与页面验收，再核对N6-07原四条remaining及原始七项浏览器验收条件。不能用本轮Windows成功覆盖Linux范围，也不把N7全阶段矩阵重复扩入N6。原主线2～3周低置信度估计未改变。
