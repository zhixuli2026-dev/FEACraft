# Agent 记忆、协调额度与真实流式收尾

## 2026-11-21 追加、压缩与提交恢复

正式 Agent 的 `update_memory` 使用与普通文件提案共用的暂存、配额和恢复日志。由同一个 SQL 事务记录文件提交见证和成功工具结果。提交前复核当前用户、会话执行凭证、工具参数和源文件摘要。旧文件的 SHA-256 在写事务外按 64 KiB 分块计算，内存只保存生成新文件需要的正文。单次追加上限 16 KiB，写入结果上限 64 KiB。

- SQLite 本地文件与 PostgreSQL 认证 SeaweedFS 的压缩和主动取消均通过。旧记忆首块暂停期间，独立连接可获得同案数据库写锁。压缩后的实际内容与保留规则一致。启用的索引上报新摘要，公开搜索可检索保留内容，早期条目已移除。主动取消在文件工具仍读取时送达，原文件保持原字节，工具记录为已取消。[本地结果](../../tmp/live-deepseek-edit-memory-28rc01jw/live-result.json) · [认证 S3 结果](../../tmp/live-deepseek-edit-memory-zf01rvom/live-result.json)
- 本地并发追加在第一个请求完成推导后暂停。另一个会话先写入，第一个请求遇到旧版本后重新推导，两个模型回合各保存一条记录。[本地并发](../../tmp/live-deepseek-edit-memory-blkf5ms8/live-result.json)
- 认证 S3 并发追加等待当前持有者结清写入。首轮发现第二笔写入可打断第一个会话续答的记忆读取。复验中提示读取沿用原期限等待存储租约，两个真实会话均完成续答，两条记录各出现一次。[首轮并发](../../tmp/live-deepseek-edit-memory-5qstfi73/live-result.json) · [认证 S3 并发复验](../../tmp/live-deepseek-edit-memory-1qprke93/live-result.json)
- 本地 API 在 SQL 提交前及提交后实际退出。冷启动前者回滚原内容并记录一次中断，真实新回合可以追加一次。后者恢复已提交内容和唯一成功工具结果。两种情况均拒绝重复批准原工具。[本地退出](../../tmp/live-agent-memory-recovery-omvr19rj/live-result.json)
- PostgreSQL 与认证 SeaweedFS 通过相同的两次实际退出。Edge 从本地退出后的数据库快照打开对应会话，显示唯一已完成的记忆工具卡。[认证 S3 退出](../../tmp/live-agent-memory-recovery-85uw5imd/live-result.json) · [Edge 历史](../../tmp/memory-recovery-browser-e4lia731/browser-result.json)
- 正式追加及新会话回忆已在 PostgreSQL 与认证 SeaweedFS 上通过。真实 Plan 权限验收核对工具清单，并由官方接口生成受限工具调用以验证服务端拒绝。浏览器的权限状态与真实服务一致。[追加和回忆](../../tmp/live-deepseek-edit-memory-w5oauzyh/live-result.json) · [Plan 权限](../../tmp/live-plan-permission-2r2i6gpg/live-plan-permission-result.json)

FF-F36 已验收。两种存储各经过实际压缩、取消、并发写入和提交前后退出。压缩通过 225,080 B 旧内容，首块 65,536 B，新正文 65,518 B。流式推导与原规则对比 134 项。合计 46 次真实 DeepSeek HTTP 200，其中通过组 31 次，真实服务上报 382,364 tokens，模拟 0。首轮验收补齐了索引启用步骤，并按实际工具 ID 统计恢复后的唯一追加。原始失败和通过结果均已登记。[汇总](../../tmp/FF-F36-memory-acceptance-20261121/validation.json)

## 2026-11-21 只读取注入模型的记忆范围

Agent 组装模型提示时读取 `memory.md` 的保留尾段，沿用文件版本检查和原 64 KiB 保留规则。旧文件超过上限时，读取范围从文件末尾反算并按行整理。模型提示读取保持只读，旧文件原字节可经正式文件接口读回。

- 文件系统实测缺失、空文件、普通记录和两种 Unicode 大文件，保留文本与原有压缩规则一致。[本地检查](../../tmp/prompt-memory-tail-9iaif2dz/result.json)
- PostgreSQL 与认证 SeaweedFS 实测 147,081 B 旧记忆。Agent 读取末尾 65,508 B，发给真实 DeepSeek 的请求中含近期标识，已淘汰标识未出现。模型逐字返回近期标识，整个源文件复查一致。[尾段和实际推理](../../tmp/live-deepseek-edit-memory-k77iqc23/live-result.json)
- `update_memory` 追加及新会话回忆通过另外三次真实推理。该组后续旧文件的种子写入遇到过时的验收断言。更新观察器以识别当前存储租约，再运行尾段验收通过。首次运行还修复了测试夹具缺少 `ToolExecutor` 导入的问题。模型共 4 次 HTTP 200、28,609 tokens，模拟 0。[追加、回忆与被拒的种子写入](../../tmp/live-deepseek-edit-memory-f25gel52/live-result.json) · [启动失败记录](../../tmp/live-deepseek-edit-memory-01_3j7vt/live-result.json)

本轮结果覆盖模型提示读取。追加、压缩、写入结算及退出恢复见上方的后续记录。

## 2026-11-06 真实流式收尾

2026-11-06，本批补齐 Agent 消息接收、执行启动、拒绝审批、会话删除和记忆读取的存储恢复额度，并通过真实模型发现、修复了断线后会话无法释放的问题。全部模型回合使用授权的 DeepSeek profile 26，未模拟模型回复、工具选择或 CLI 推理。

## 实现变化

消息、拒绝、删除和执行所有者领取在进入算例协调器之前获取工作区所有者额度，保持到本次 SQL 提交；图片内容也按工作区实际所有者核算。审批领取阶段独立受保护，文件审批复用已有额度，原生任务领取结束后释放额度，不把文件额度带入未来原生任务。

记忆读取与三个文件读取工具共用 `agent_storage_capacity.read_case`：独立 SQL 读会话取得协调锁和恢复额度，在线程中读取后结算，再调用模型。旧记忆读取在模型请求期间仍保留父事务中的算例锁，本批已解除这个持锁范围；主会话 ORM 状态不受读会话回滚影响。

真实客户端断开 SSE 后，旧逻辑的异步数据库/会话收尾被外层取消再次打断，持久执行状态仍 active、内存注册仍存在，30 秒后仍不能续聊。[首次失败](../../tmp/live-deepseek-control-y1z043fn/live-result.json)及同目录 API 日志保留。修复在必要清理段使用 AnyIO 的取消屏蔽，保证关闭数据库、停止并等待 provider 迭代任务、记录中断、释放执行所有者及移除注册；显式关闭内层生成器，且不再在生成器 finally 中向已断开的客户端 yield。AnyIO 的重复取消及异步收尾要求见[官方说明](https://anyio.readthedocs.io/en/stable/cancellation.html#finalization)；屏蔽只用于清理，未屏蔽正常推理过程。

## 真实验收

| 验证组 | 实际推理请求 | 结果 |
| --- | ---: | --- |
| SQLite/S3：文本替换批准、拒绝，记忆追加、新会话回忆 | 6 | [全部通过](../../tmp/live-deepseek-edit-memory-q6sch_sw/live-result.json) |
| PostgreSQL/S3：同四类操作 | 6 | [前三类通过，首次回忆漏字失败](../../tmp/live-deepseek-edit-memory-5trra41z/live-result.json) |
| PostgreSQL/S3：仅回忆复验 | 1 | [通过，并确认实际请求含完整记忆标识](../../tmp/live-deepseek-edit-memory-1gr8y1n_/live-result.json) |
| 修复前真实取消/断线 | 3 | [主动取消通过，断线占用未释放](../../tmp/live-deepseek-control-y1z043fn/live-result.json) |
| 修复后真实取消/断线及各自续聊 | 4 | [全部通过](../../tmp/live-deepseek-control-c2qa50u7/live-result.json) |
| 收尾改动后普通记忆工具与回忆兼容 | 3 | [通过](../../tmp/live-deepseek-edit-memory-8_vii0du/live-result.json) |

文本替换保留文件中未修改的前后文；拒绝不改文件。记忆内容通过正式文件接口核对，回忆使用新的会话，用户提示未提供验证字符串。首次 PostgreSQL 回忆中，文件实际存储正确，模型输出随机标识时漏了一个字符；保留失败，不降低精确匹配标准。单项复验增加了对实际发给模型的请求的观察，并明确逐字复制，结果通过；这不证明模型总能逐字复制任意内容。

额度验证把隔离账户存储额度设为零，消息接口在保存用户消息或触发模型前返回 429；恢复额度后正常执行。观察器在每次实际 S3 恢复 fence 时断言额度存在，在真实 DeepSeek 网络请求时断言已退出额度，没有替换返回结果。通过组还验证会话删除和额度结算。PostgreSQL 两个临时库已移除：[第一组](../../tmp/live-approval-postgres-3ga0faii/cleanup.json)、[回忆单项](../../tmp/live-approval-postgres-r35zv12y/cleanup.json)。

流控制在收到真实正文后才发送取消，或直接关闭实际 HTTP 流。修复后两种情况均等待到持久状态 idle、内存注册清除，历史为 cancelled/interrupted，再在同一会话发起实际推理并完成回答。此证据证明本平台收尾和续聊行为，不宣称能够核验上游厂商内部计算或计费何时终止。

本批 23 次实际推理请求。19 次有最终 usage，合计已报告 79,669 tokens；4 次被取消/断开，未返回最终 usage，不能计作零消耗。累计实际推理请求 62 次，已报告 tokens 至少 252,520；失败场景保留，不能把全部请求说成全部验收通过。所有隔离进程已退出，开发库与原会话未修改。

## 剩余范围

本批没有核验全部图片与共享工作区组合、原生审批启动、数据库写入中途断线、Goal Runner 的全部中断阶段、跨进程/多节点恢复；这些仍需各自真实或实际基础设施验收。文本替换同步预检辅助函数仍按本地路径读文件，正式审批执行已使用实际存储，旧辅助入口需后续清理。报告/几何等其他读写、上传预留交互、ZIP/备份/导出峰值，以及 RB-13 至 RB-17 继续推进。

主线仍 33/53 完成、剩余 20，资源块 11/17 完成、剩余 6；295 项全功能范围保持不变。复验入口为 `live_llm_edit_memory_acceptance.py` 和 `live_llm_control_acceptance.py`，均须显式提供授权 profile；可按 `--modes` 仅执行需复验的场景。
