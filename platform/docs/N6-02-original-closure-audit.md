# N6-02原条件退出审计

2026-11-12。原任务：**outbox 联动文件树、索引和产物发布，保留轮询对账兜底**。

原remaining全文保持于台账closed_remaining：

> 承接 N2-06：文件/SQL/索引对账、outbox 丢失后补偿和产物发布联动；索引失败不能改写专业求解结果。

全部53项和295项目标继续有效。本审计只关闭原N6-02，不代表全产品、完整部署矩阵或295项已交付。最终源包、证据摘要及关闭计数见`tmp/mainline-N6-02-closure-evidence/validation.json`。

## 原要求与实际证据

| 原要求 | 实现与实际核验 |
|---|---|
| outbox缺失补偿 | dispatcher按100条主键页、每轮固定上界扫描已提交DurableEvent，仅唯一插入缺失意图，不覆盖原租约、重试时间或回执。V1实际提交文件后删除pending意图，同事件ID/序号/摘要补建投递成功；V4实际API退出/启动后再次验证同一缺失事件。 |
| 文件、SQL、索引一致 | 清除文件PUT/上传/完成/删除、Agent提案与同步工具、记忆、caseDict及复制/导入的提交前索引旁路；事件消费者和索引读取协调业务文件事务，并排空索引线程后释放锁。V3实际写入后回滚SQL，文件恢复为不存在，FTS未收录未提交内容，也无提交事件。 |
| 没有事件仍能收敛 | 独立五秒浏览器读取不依赖WS或事件补拉成功。V1两个事件通道都失败时实际新文件出现在树中；V3保持大小、inode和全部记录的时间戳不变，直接修改真实文件，暂停dispatcher且断开两个事件通道，原始FTS约58秒后更新，未新增事件/消费回执。 |
| 索引自身对账 | 已启用索引根据高精度本地/完整远端元数据检查变更，每60秒再核对实际文本；一致时保留构建身份/回执，不自动建立未启用索引。真实FS的build祖先不再误排除整个case；真实SeaweedFS替换、删除、新增及排除目录通过，未变化时构建身份保持。 |
| 产物发布联动 | V3实际Pandoc作业19/20生成HTML/DOCX，断开事件通道时阅读器独立更新过期/历史状态，旧正文固定，明确打开最新后显示新原生正文；文件树显示产物路径，原始FTS核验report.source-19.md/20.md已收录。build目录仍按既有策略排除，不声称HTML/DOCX二进制均进入全文索引。 |
| 索引故障不改写专业结果 | 真实索引SQLite写锁下，Pandoc19及CalculiX21均成功，原生停止已核验；索引状态返回500、消费者保留pending OperationalError。释放锁后相同事件投递并产生索引回执，不重跑原生作业。CalculiX真实C3D8模型8节点位移、固定端和对称性断言通过，FRD的3610字节在索引故障前后完全相同，SHA256为`c7337ecdf032764bb72059ded9101de1cd412d606e031e6aa86322a1eb15e34e`。 |
| 真实Agent链路 | 授权profile26，官方api.deepseek.com，实际deepseek-flash两次HTTP200、两条真实流身份及10422 tokens。真实文件提案审批后写入E-report/report.md；相应提交事件投递一次，原始索引中有同事件回执和实际文本，随后实际原生报告包含该文本。 |
| 浏览器权限和迟到响应 | viewer实际case/工作区FTS、索引状态、事件HTTP/WS可读，重建被拒绝；撤权关闭WS4403并清理路径，迟到真实HTTP200不能恢复受保护结果。跨case的实际搜索/索引迟到响应被隔离；跨账户、相同case的真实旧账户搜索响应交付后也未进入新账户。实际回收站case从工作区及直接搜索排除。 |

## 固定三组与证据入口

1. 持久事件、补偿与文件/SQL/索引收敛：`tmp/case-workflow-controls-98nf19rk/controls-result.json`、`tmp/case-workflow-controls-urvmuuc0/file-index-rollback-v3.json`、`external-storage-reconciliation-v3.json`、`tmp/N6-outbox-actual-databases-v3-20261112/result.json`、`tmp/N6-current-index-S3-v4-20261112/result.json`、`tmp/case-workflow-controls-80ysbjpl/actual-API-restart-index-repair-v4.json`。
2. 独立浏览器读取、授权、迟到响应及发布阅读：两轮`event-reconciliation-browser-evidence-v3.json`/`v4.json`记录完整实际驱动与输出；前者位于`tmp/case-workflow-controls-urvmuuc0`，后者位于`tmp/case-workflow-controls-80ysbjpl`。最终按钮在1280px/375px及深浅主题下检查，14px文字、至少44px目标、键盘焦点及13.4:1/17.7:1对比度通过。窄屏仅证明本控件可用，完整移动工作台仍属原N6-07。
3. 真实模型/原生结果与退出：V3`upstream.jsonl`、`live-agent-proposal-events-v3.json`、`live-agent-approved-events-v3.json`、`index-contention-*.json`；V4`actual-calculix-isolation-v4.json`、`calculix-index-failure-observation-v4.json`、`calculix-index-recovered-v4.json`、`actual-calculix-output-exact-v4.frd`及上述重启证据。两轮`controls-result.json`均实际核验API/preview/终端和新增原生退出、旧workflow/step/job/attempt历史及来源数据库保持，V4另核验被复制的原索引库保持。

最终运行审查为`tmp/N6-event-reconciliation-review-v4-20261112.json`，运行包SHA256 `9b3827579fa2780afd27673d912172abad2a17cf57268aef70679907897cc232`。应用和实际辅助代码冻结并受哈希检查；原生所有者活着时未修改生产源码。S3驱动另有完整辅助代码审查与出生身份停止核验。

## 计数、失败与验收限制

- 本项累计真实模型2次，模拟LLM0次。新增真实原生Job3个：Pandoc19/20、CalculiX21；复制的原18个历史Job不计新增。V4重启和控件检查没有新增模型回合，未用旧模型结果假装新推理。
- 双库各207条是显式SQL协议元数据，用于分页上界、插入竞争、租约/回执保持及回滚边界，不是模型回复或业务Job。WS/HTTP中断、真实响应延迟、实际索引锁及删除投递意图分别归为故障检查。
- V1共享workspace搜索404和提交前索引旁路未算通过；后续已修复并实际复验。V2启动遗漏旧构造器导入导致失败，原失败目录`tmp/case-workflow-controls-x9qb578x`保留，未发出模型请求。V3首个报告提交409、同幂等键后续接受为唯一作业19，最初响应体未保留，不推断原因；Case2索引未复制的测试准备错误及Playwright证据写入动态import失败均保留，未重跑已发生的故障来覆盖结果。
- 新按钮原白字蓝底实际对比度失败已保留；V4用既有主题色复验通过。前端构建通过，lint有现存及相关effect警告，不宣称零警告。历史模拟LLM集合没有执行；CalculiX旧脚本只经AST提取工程输入常量，没有导入或运行其顶层测试。
- 此处是本机实际API退出/启动与持久副本演练，不是全物理多主机、全部SQL/S3组合或强制宕机矩阵。S3本轮证明实际索引后端，不替代全部API额度/部署验收。统一资源上限、远端worker接管、完整入口/PTY/移动界面及全部故障/性能/迁移/部署矩阵继续原N4、N6-07、N7；未把这些原责任改名关闭。商业登录正式配置仍按用户要求暂缓。

295条全功能台账的完整交付规则继续有效，未借本主线项批量标为完成。下一项为原N6-07。
