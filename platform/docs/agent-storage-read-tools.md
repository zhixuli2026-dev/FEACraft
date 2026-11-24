# Agent 使用实际存储后端读取文件

## 2026-11-21 扫描预算、并发保存和取消

Agent 的三个文件工具沿用正式权限检查。取得水位和存储版本后结束读取事务，正文由共享的有界读取池执行。发布结果前复核账号状态、算例权限和版本。发生同案写入时最多重新读取一次。取消会阻止再取下一块，并等待已开始的读操作收尾。

- 本地遍历在进入 `node_modules`、`build`、缓存和归档内部目录前排除它们。S3 旧目录使用带分隔符的分页列表。当前 S3 算例读取一份版本清单，按其中的物理键和 ETag 读取匹配文件。
- 列表保留前 500 项，搜索保留前 50 条。搜索跳过超过 5,000,000 B 的单文件，单次累计正文扫描达到 32 MiB 后提示缩小目录。预览保留最多 50,000 字符。现有的 `subdir`、`include_internal` 和空文件行为通过回归。
- SQLite 本地文件和 PostgreSQL 认证 SeaweedFS 各调用 DeepSeek `deepseek-flash` 6 次。三个工具均由真实模型选用，模型续答含随机验证值，结果落库，源文件保持原样。[本地文件](../../tmp/live-deepseek-read-tools-0q6y8iz8/live-result.json) · [认证 S3](../../tmp/live-deepseek-read-tools-nb50sfwp/live-result.json)
- Edge 从另一次真实模型验收的隔离 SQLite 快照打开三个会话，工具卡均为已完成，已保存的回答可见。该轮的 6 次正式推理已成功，浏览器起初缺少 Python Playwright，补齐依赖后使用原快照完成回放。三次模型验收合计 18 次 HTTP 200、77,900 tokens，Edge 回放新增模型请求 0 次。[原会话及接口证据](../../tmp/live-deepseek-read-tools-kcw1j10o/live-result.json) · [Edge 回放](../../tmp/agent-read-history-browser-bnsmzams/browser-result.json)
- 实际文件暂停在首块时，两组均可在同案保存，随后重读。S3 还验证同一文件替换后的新版本。取消只读出首块，排空后重试通过。读取中停用账号返回 403，未返回正文。[SQLite](../../tmp/agent-read-scope-m50wwbso/read-scope-result.json) · [PostgreSQL 和 S3](../../tmp/agent-read-scope-jichtb08/read-scope-result.json)
- 实际 SeaweedFS 搜索记录一次清单 GET、一次正文 GET。旧目录以两个实际分页返回 130 个可见文件，没有请求 `build` 子目录。实际本地文件另通过 13 项边界检查，包括缩小目录后找到超出全局扫描预算的结果。[S3 请求记录](../../tmp/agent-read-scope-jichtb08/read-scope-result.json) · [本地边界](../../tmp/agent-read-boundaries-oqzm_l43/result.json)

模型验收使用 `server/live_llm_read_tools_acceptance.py --source-profile-id 26 --storage s3 --postgres`，本地使用 `--storage fs`。已安装 Python Playwright 和 Edge 的环境可用 `--dist` 指定前端构建。纯存储检查使用经审查的 `server/agent_file_read_boundary_checks.py`，可选 `--scope fs` 或 `--scope s3 --postgres`。两类验收都未使用模拟推理。RB-14 的整个长 I/O、锁和心跳范围仍由 N4-05 跟踪。

首次启动时，记忆读取调用仍使用旧参数，两个隔离会话在推理发出前中断。更新调用并重新运行后，三个文件工具均通过。[本地初次运行](../../tmp/live-deepseek-read-tools-n_sdztea/live-result.json) · [S3 初次运行](../../tmp/live-deepseek-read-tools-7azrtmqb/live-result.json)

## 2026-11-06 首次实际存储联调

2026-11-06，真实 DeepSeek 在 S3 算例中调用 `read_text_file`，旧工具因只检查本地路径而返回“文件不存在”。该文件实际已通过正式 API 写入 S3，隔离服务没有本地镜像。[首次失败记录](../../tmp/live-deepseek-read-tools-88f30f1t/live-result.json)保留了两次真实推理与错误工具结果，不计通过。

## 实现

- `read_text_file` 使用共用 `FileRead`，按当前 FS/S3 后端选择文件版本。预览保留最多 50,000 字符；大文件只读取最多 200,004 字节的前缀，并显示完整文件字节数。S3 读取校验版本和 Range 响应，本地读取检查文件身份。
- `list_files` 与 `search_text` 使用存储层的当前逻辑目录，返回相对算例根目录的路径；不需要创建或刷新本地 S3 镜像。两者共用缓存/构建目录排除规则，显式显示 `.vibeflow` 时仍排除其内部构建缓存。
- 搜索保留大小不超过 5 MB 的文件、最多 50 条匹配及行号；遇到存储或版本错误返回工具失败，避免把故障当成“无匹配”。列表输出最多 500 项，确实存在第 501 项时才提示截断。
- 正式 Agent 调度在独立读会话中取得算例协调锁及读取恢复额度，并在线程中执行、等待结束。读会话回滚和额度结算不会使主会话中待保存的工具/会话 ORM 对象失效；读取结果仍由主会话保存。读取不修改算例源文件。

## 验收证据

全部模型回合使用现有授权 DeepSeek profile 26、`deepseek-flash`，经正式平台消息接口执行，没有模拟 provider、模型输出或模型工具选择。

| 验证 | 结果与证据 |
| --- | --- |
| S3 + SQLite，无本地镜像 | 真实模型分别选择读取、列目录、搜索并续答，6 次推理通过；[记录](../../tmp/live-deepseek-read-tools-xiy0zni6/live-result.json) |
| 本地文件 + SQLite | 同三个模型回合兼容，6 次推理通过；[记录](../../tmp/live-deepseek-read-tools-j3ptshon/live-result.json) |
| S3 读取额度结算 | API 退出后只读核对隔离 SQLite，9 条相关预留均 settled；[记录](../../tmp/agent-read-credit-settlement.json) |
| 文件工具边界 | 9 项通过，直接操作临时真实文件，无 LLM 调用；[记录](../../tmp/agent-read-boundaries-zh8nc0e7/result.json) |

真实模型测试要求工具结果和最终回答均含仅存在于文件中的随机验证值，或实际生成的随机文件名；同时核对缓存目录排除、工具历史落库和源文件未变。边界检查另覆盖空文件、Unicode/大文件预览、三个工具各四种非法路径、显式内部目录、搜索跳过超限文件、500 项列表截断。它们是文件实现证据，不作为模型能力证据。

修复后通过场景共 12 次推理、51,456 tokens；首次缺陷复现 2 次、8,613 tokens。本批合计 14 次、60,069 tokens；累计真实推理 39 次、172,851 tokens，其中缺陷复现场景保留为失败，不能宣称所有场景通过。隔离 API 和 SeaweedFS 均已退出，开发库只用于读取已授权凭据，没有修改开发账户或文件。

显式入口为 `server/live_llm_read_tools_acceptance.py --source-profile-id 26 --storage s3`，本地兼容使用 `--storage fs`；可用 `--modes read,list,search` 选择相关组。`server/agent_file_read_boundary_checks.py` 仅验证真实文件边界，不调用模型，不进入历史模拟回归集合。

## 首次联调时的剩余工作

本批不等同于全功能交付。PostgreSQL 下的独立读取会话、读取期间取消/断线、外部并发变化和存储故障还需要对应验收。列表底层仍构造完整存储清单，搜索没有全目录累计扫描预算；500 项和 50 条限制只是输出上限，完整扫描预算仍属 RB-14/RB-15。Agent 的消息/记忆读取和拒绝审批等其他可能触发恢复的协调入口尚未全部接入额度。文本替换、记忆更新、完整工程任务及全功能清单继续推进。

主线仍为 33/53 完成、剩余 20；资源块 11/17 完成、剩余 6；295 项全功能范围保持不变。
