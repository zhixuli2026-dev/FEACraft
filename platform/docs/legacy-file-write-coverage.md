# 算例可见文件的旧写入口覆盖表

后续验收：下文保留发现缺口时的入口表；五类可见写入现已全部迁移并通过四组合60组真实中断验证，N2-06按原条件关闭，见[最终实现与证据](visible-file-transactions.md)。N2-02/N2-07及其他阶段仍继续。

2026-11-06。N2-06 收口使用实际函数及其调用者核对，不再用“旧专业准备/批次清单”泛称所有未完成工作。本表依据当前 `app/routers`、事务相关服务、执行入口以及直接存储写入调用核对。它不是对整个项目所有磁盘写入的原子性承诺：节点配置、索引、临时原生工作目录和账户级共享配置有各自的原任务。

## 已接入的调用链

| 入口/写入域 | 当前实际路径 | 崩溃判定与证据 |
| --- | --- | --- |
| 普通文件、上传、删除 | `routers/files.py` → `file_transactions` / `s3_file_transactions` | 前后镜像 + 同事务 SQL 事件；本地/S3 文件专项 |
| 字典、状态镜像、时间线和修订 | `casedict.write_case_dict` → 同一文件事务 | 不拆分正文/镜像/历史提交；字典事务专项 |
| Agent 文件提案/替换和 memory | `ToolExecutor.execute_case_tool` → `apply_proposal` / `write_memory` | 正式 API 在工具事件提交后收尾；保留同步工具 helper 不代表 API 绕过日志 |
| 分块上传完成 | `uploads.complete_session/finish_completed` 及 S3 适配 | 文件事务 + 上传 SQL 完成回执；续传/清理专项 |
| 新建/ZIP导入/算例与工作区复制/备份恢复 | `case_provision.create_files/create_from_manifest` | 账户锁、创建清单、目录占用与提交证明；创建/构造专项 |
| UDF 九文件模板 | `routers/udf.scaffold` → `file_transactions.write` | 本批接入；实际 API 首文件/提交前后终止恢复 |
| 批次 manifest/index | `routers/jobs.submit_batch` → `file_transactions.write` | 本批接入；与 Job/attempt/投递意图同事务；恢复及幂等重放专项 |
| UDF、MPI、FloTHERM 库等生成配置 | 对应 handler `prepare` → `generated_inputs` → attempt 封存 | 不再在旧工作区原地准备配置；封存计划/SQL证明及对应专业专项 |
| 旧后处理、文档原生准备 | `case_read_view`、独立 attempt 及持久准备证明 | 私有缓存有归属；未形成完整证明的 finalizing 补交继续 N5-04 |
| 旧作业以及终端/viz/Agent Shell/CLI 的 S3 staging 回传 | `staging_sync.sync_job/sync_authorized` → `sync_checked` | 持久 storage operation、前后镜像、SQL事件及永久 CAS 指针；批次15–20证据 |
| 不可变专业产物与旧别名 | publication journal/objects + alias plan/recovery | 已提交指针决定可见版本；新修改/新代次不被旧恢复覆盖 |

`storage.sync_job_staging` 当前在 app 中只有定义，没有产品调用；不能再把它当作五个会话入口正在使用的旧写入。`case_events.append_case_event`、`provenance.record_case_revision` 和 `report_build.publish_report_artifacts` 的直接写辅助函数当前在 app 中未发现调用；保留其存在不构成新请求使用它们的证据。`update_case_dict` 仍作为 Agent 同步工具回调传入，但正式提案路径在 `execute_case_tool` 中转到异步 `write_case_dict`；同步辅助工具不得替代该正式准入。

## 还没有日志覆盖的五类可见写入

| 入口 | 已核对的直接写函数/目标 | 下一实现和验收 |
| --- | --- | --- |
| CAD 场景 WebSocket | `cadscene._write_scene` → `storage.put_text`，`A-geo/scene.json` | 保留每次写入的新授权与哈希条件，改用文件事务；中断后场景/事件一致，广播不早于提交 |
| STEP 几何修复 | `geometry._repair_locked` → `put_bytes` + `put_text`，修复 STEP/validation 两文件 | 将两个文件作为同一事务；首文件后中断不能留下可用新 STEP 配旧证明 |
| Gmsh 预览 | `geometry._preview_gmsh_locked` → `put_bytes`，`.vibeflow/msh-preview/.../preview.vtu` | 接入日志，保留输入摘要、容量与原转换结果；提交窗口恢复 |
| 旧网格质量评估 | `mesh._evaluate_locked` → `mesh_quality.write_quality_report`，`B-mesh/quality.json` | 接入日志并保留原 Mesh OK/质量政策门禁；中断不能借新报告绕过输入绑定 |
| 会话消息图片 | `agent.post_message` → `storage.put_bytes`，消息图片路径 | 图片与消息事件同事务证明；缺/未知 commit 不删可能已提交的图片，不留下不可诊断孤立数据 |

这些是本轮源码确认的待改入口，不是已经复现的数据损坏，也不是新增加的五项主线任务。N2-06 remaining 用这五类具体写入替代此前泛称；原 53/295 范围、后续 finalizing/资源/索引/人工界面/部署义务不变。

完成上述迁移后，应再次核对直接写调用及已有提交窗口证据，然后按原 N2-06 条件决定关闭。若发现实际尚未覆盖的调用者，必须明确函数和写入窗口，不以宽泛的“全矩阵仍待完善”代替结论。
