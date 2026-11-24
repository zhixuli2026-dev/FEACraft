# Agent 远端提案基线与写入额度

2026-11-20，普通文件提案复用编辑器暂存。领取待审批工具前仍核对账户额度，实际处理阶段改用算例预约及逐次 S3 写入额度。真实 DeepSeek 发起的 40 MiB 手动替换、自动替换、完整覆写均通过，暂停读取时同账户另一算例可以保存。零额度仍保留待审批状态，解除限制后原提案可以批准。[普通文件提案实测](file-transaction-diagnostics.md#agent-普通文件提案)

2026-11-06，真实 DeepSeek 驱动 S3 文件修改时发现：`ToolExecutor.proposal_baseline` 从本地文件路径计算基线，远端已有文件没有本地镜像时得到 `None`。这是实际功能缺陷，不是模型不可用。[首次失败回执](../../tmp/live-deepseek-approval-r3okk6e4/live-result.json)保存了真实模型提案及错误基线；该次实际推理返回 200，场景验收失败，不计通过。

现在基线使用当前存储后端的 `file_hash`，并在线程中读取、等待其结束；审批继续在算例锁内复验。新增 `agent_storage_capacity.file_tool`：文件覆写、文本替换、记忆写入按工作区实际所有者获取 S3 持久额度。审批路径在领取工具状态前准入，自动工具路径在进入 running 前准入；额度覆盖工具执行、SQL 提交和已提交文件日志收尾，退出后才调用模型续答。Shell/原生任务不继承这一文件额度，继续使用各自资源边界。本次没有改变开发库、执行开发库迁移或重启开发服务。

## 实际验收

全部模型回合使用已授权 profile 26 的 `https://api.deepseek.com/v1`、`deepseek-flash`，没有构造模型回复、工具选择或模拟推理。S3 是隔离的真实 SeaweedFS，启用版本保存；数据库与算例均隔离。

| 场景 | 实际推理请求 | 回执 |
| --- | ---: | --- |
| S3 + SQLite：批准写入及续答、拒绝、外部修改冲突及续答；重复批准 409 | 5 | [记录](../../tmp/live-deepseek-approval-qxmkduui/live-result.json) |
| S3 + SQLite：零额度拒绝后恢复审批、full_access 自动写入及续答 | 4 | [记录](../../tmp/live-deepseek-approval-31kpq7ih/live-result.json) |
| S3 + PostgreSQL：零额度拒绝后恢复审批、full_access 自动写入及续答 | 4 | [记录](../../tmp/live-deepseek-approval-mu35s47j/live-result.json) |
| 本地文件 + SQLite：变更后的批准写入与续答兼容 | 2 | [记录](../../tmp/live-deepseek-approval-fj5o1_6s/live-result.json) |

通过场景共 15 次实际推理、72,439 tokens；另有首次定位缺陷的 1 次实际推理、4,813 tokens。本批总计 16 次、77,252 tokens。此前两批为 9 次、35,530 tokens；累计 25 次真实推理、112,782 tokens，其中首次 S3 基线场景失败已保留证据，不能说 25 次场景全通过。

零额度检查只调整隔离应用的存储配额，不替换模型：真实模型先生成待审批提案，额度设为零后审批返回 429，文件不变、没有追加模型调用、提案仍 pending；恢复额度后批准同一提案并实际续答。S3 检查还在真实文件执行时断言额度存在、在实际模型网络请求时断言额度已退出，并核对持久预留均 settled。[PostgreSQL 清理记录](../../tmp/live-approval-postgres-hce5j32z/cleanup.json)确认临时库已删除；所有 API 和对象存储进程均已退出。

复验入口 `server/live_llm_approval_acceptance.py` 增加 `--storage s3`、`--database postgresql` 以及 `--modes quota,full`。每个隔离 API 最多 8 个上游 HTTP 请求，分组运行；已通过的组仅在相关代码变更或新问题需要时重跑。

## 剩余范围

本次没有把整个 RB-12 或全部 Agent 功能记为完成。文本替换、记忆工具虽共用额度边界，仍需各自真实模型验收；取消、中途额度耗尽和恢复、共享工作区 Agent 操作等仍需对应证据。Agent 的消息/记忆读取等其他协调入口可能执行恢复写入，尚未全部接入额度；报告、几何等其他读写、上传未来额度与 ZIP/备份/导出峰值仍在 RB-12。

代码检查同时发现 `read_text_file`、`list_files`、`search_text` 仍依赖本地路径，S3 下没有本地镜像时无法读取远端事实源；下一增量需修复并由真实模型验证。主线仍为 33/53 完成、剩余 20，资源工作块 11/17 完成、剩余 6，全部 295 项功能交付范围不变。
