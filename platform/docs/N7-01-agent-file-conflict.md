# V03 文件审批后并发编辑

2026-11-18 在隔离算例中复现并修复了 Agent 文件审批期间的覆盖问题。DeepSeek 产生带原文件 SHA-256 的真实写提案。用户批准后，在算例已通过第一次摘要检查、文件事务即将捕获原件时，独立编辑器保存另一份内容。修复前工具报告成功，最后读回的是模型建议的内容。[首次覆盖的文件和工具回执](../../tmp/live-deepseek-approval-n1kkye_v/live-result.json)

## 写入与恢复行为

普通写入、文本替换和受保护的 caseDict 写入将提案摘要传递给文件事务。事务在捕获原件时再次比对。原目标不存在时，显式空摘要要求目标仍不存在。旧提案缺少摘要字段时，审批记录会说明需要重新读取并提案。普通文件继续走原提交日志，caseDict 的正文、数据库镜像和修订审计仍由专用事务处理。S3 在上传该文件的恢复副本之前检查捕获的内容。[工具执行](../server/app/services/tools.py)、[文件事务](../server/app/services/file_transactions.py)、[S3 实现](../server/app/services/s3_file_transactions.py)

## 最终复验

| 后端及入口 | 同一轮中观察到的结果 | 回执 |
| --- | --- | --- |
| S3 / PostgreSQL | 整文件写入和文本替换在批准后均遇到外部修改，外部内容保留。另一个无冲突提案成功写入。重复批准返回 409，无额外推理，存储预留结清。 | [实际 API、文件与模型记录](../../tmp/live-deepseek-approval-snv199og/live-result.json)、[临时库清理](../../tmp/live-approval-postgres-mf2dyorc/cleanup.json) |
| S3 / SQLite | 针对 caseDict 的文本替换发生冲突，外部正文保留，数据库修订与审计时间线保持原值。创建提案等待时由外部先创建文件，审批也保留外部文件。 | [两种冲突及持久记录](../../tmp/live-deepseek-approval-meb50uam/live-result.json) |
| 文件存储 / SQLite | 批准前已修改、批准后修改和无基线旧记录三种情况产生明确失败原因。旧记录使用真实模型提案，仅在隔离数据库中移除旧版本缺失的基线字段。所有冲突均保留原提案参数，未触发作业、几何发布或目标文件的成功变更事件。 | [实际调用和历史记录](../../tmp/live-deepseek-approval-_lexymzi/live-result.json) |
| Edge / 文件存储 / SQLite | 从 Agent 面板点击一次“批准执行”，外部编辑在审批过程中落盘。卡片显示失败和需重新读取的原因，刷新页面后仍显示同一原因。按钮消失，全程仅一个批准请求。 | [浏览器断言](../../tmp/live-deepseek-approval-yyqg2buf/browser-conflict.json)、[画面](../../tmp/live-deepseek-approval-yyqg2buf/approval-conflict.png)、[模型调用与文件读回](../../tmp/live-deepseek-approval-yyqg2buf/live-result.json) |

以上四轮合计 18 次真实 DeepSeek HTTP 200，88,683 tokens。模型实际选择工具并在审批后续答。文件、数据库、持久事件及浏览器结果独立核验。另有中间实现的 28 次调用与首次复现的 2 次调用。本批共 48 次 HTTP 200，236,279 tokens。首次复现的功能结果为失败。[中间 FS](../../tmp/live-deepseek-approval-jaeexpgg/live-result.json)、[中间 S3](../../tmp/live-deepseek-approval-827le9kb/live-result.json)、[旧记录与创建](../../tmp/live-deepseek-approval-5p6gqx89/live-result.json)、[S3 字典](../../tmp/live-deepseek-approval-ym8ama9g/live-result.json)、[FS/PostgreSQL](../../tmp/live-deepseek-approval-pizmx0ic/live-result.json)

复验入口为 `live_llm_approval_acceptance.py`，使用已授权的 profile 26。浏览器场景参数为 `--modes race --browser`，需要已构建的 web、Playwright 及 Chromium。服务端场景可选 `--storage s3 --database postgresql`，输出保存在 D 盘临时目录。日常服务未更新。此次验收覆盖审批前以及通过首次校验、进入文件事务时两个明确的编辑时序，其他发布和主机故障继续由各自矩阵项跟踪。
