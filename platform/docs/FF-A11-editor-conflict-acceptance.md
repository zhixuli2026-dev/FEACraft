# FF-A11 编辑冲突恢复与 FF-A19 整文 caseDict 保存

普通文件编辑器提交打开时的 SHA-256。遇到外部修改时保留本地草稿，经用户确认后可使用服务器内容或以刚查看的版本继续编辑。文件已删除时，编辑器用同一算例的授权版本接口确认不存在。选择保留草稿后，下次保存附带显式 `expected_sha256: null`。如果另一进程已重建同名文件，保存返回 409，服务器文件与本地草稿均保留。若目标仍不存在，同一次保存可完成重建。放弃已删除文件的草稿需要明确确认。[编辑器界面](../web/src/panels/EditorPanel.tsx) · [编辑状态](../web/src/store.ts) · [文件 API](../server/app/routers/files.py)

Monaco 整文保存 caseDict 时继续提交专用 API、原 SHA-256 和原修订号。实际浏览器遇到外部修订后确认采用服务器版本，恢复原字节，再次保存后读取到两次连续修订和与原文相同的结构。普通编辑无法推进工程阶段，拒绝时保留草稿。独立账户无法读取，获准共享的只读账户可查询当前版本，服务端拒绝其保存。[原写入协调与迟到回执回归](case-write-coordination.md) · [Agent 审批后的独立并发验收](N7-01-agent-file-conflict.md)

## RB-14 编辑器与文件树读取

文本读取先在原算例权限和协调范围内选定对象。正文与摘要在协调事务结束后使用有界工作线程，从同一个选定版本每次读取最多 64 KiB。完整文本仍会合成编辑器 JSON。实测原始 65,548 字节同时包含跨分块 UTF-8、无效字节和不完整结尾，正文按 UTF-8 替换解码，SHA-256 与原字节一致。正文读取在测试屏障等待时，第二个 API 能在同一算例提交新文件，原请求返回 409，重读取得新内容。[文件快照](../server/app/services/file_read.py)

整树和路径增量请求共用一次读取期限。整树的可见条目和已提交事件水位仍由原算例协调范围保护。测试在执行中的真实整树工作线程内暂停，缩短该轮期限。阻塞时健康入口仍可用，恢复线程后请求返回 `file_tree_timeout`，下一次读取取得完整树和事件水位。[读取期限](../server/app/services/case_read_capacity.py) · [文件树刷新验收](FF-A12-tree-delta-acceptance.md)

## 验收记录

| 数据库与存储 | 浏览器、权限与有界读取 | 模型回执 |
| --- | --- | --- |
| SQLite 与文件系统 | [结果](../../tmp/live-cli-startup-d2_vclz0/editor-conflict.json) · [删除后的草稿](../../tmp/live-cli-startup-d2_vclz0/editor-deleted.png) | [回执](../../tmp/live-cli-startup-d2_vclz0/live-result.json) |
| PostgreSQL 与文件系统 | [结果](../../tmp/live-cli-startup-agsk4hgj/editor-conflict.json) | [回执](../../tmp/live-cli-startup-agsk4hgj/live-result.json) |
| SQLite 与 SeaweedFS | [结果](../../tmp/live-cli-startup-tj9giwej/editor-conflict.json) | [回执](../../tmp/live-cli-startup-tj9giwej/live-result.json) |
| PostgreSQL 与 SeaweedFS | [结果](../../tmp/live-cli-startup-qwv12xnl/editor-conflict.json) | [回执](../../tmp/live-cli-startup-qwv12xnl/live-result.json) |

四种组合均通过五组 Chromium 编辑断言，加上真实存储读取、权限和超时检查。最终四组编辑验收共 8 次真实 DeepSeek HTTP 200，二进制下载回归另有 2 次 HTTP 200。更早一次 S3 编辑成功回执另有 2 次，整阶段共 12 次 HTTP 200，模拟调用为 0。五个最终测试 API 均停止。S3 和 PostgreSQL 使用各自的隔离实例与临时数据库。[早期 S3 通过回执](../../tmp/live-cli-startup-n9jl8xsb/live-result.json) · [PostgreSQL 及 S3 清理](../../tmp/job-repeat-postgres-xssg00xd/result.json) · [对象存储退出](../../tmp/s3-attempt-store-uqe5cwak/job-repeat-run.json)

受影响的二进制下载入口已在 PostgreSQL 与 SeaweedFS 上复验。Chromium 下载与确切文件哈希通过，平台 Range 响应及五次直连签名请求通过。API 重启后，先前签名 URL 仍能读取其选中的旧文件版本。[文件与浏览器回执](../../tmp/live-cli-startup-bunzeqrt/file-download.json) · [模型回执](../../tmp/live-cli-startup-bunzeqrt/live-result.json)

早期浏览器测试在树载入时误收起了默认展开的目录。S3 复验曾以 5 秒截止服务器版本读取，也曾将保存前的后台请求当作保存回执。最终测试等待实际版本视图，并通过第二个 API 在保存完成后读取确切修订。测试中的短暂存储准入冲突记录在浏览器 JSON 中，仅对明确的准入冲突再次尝试。[初次目录选择失败](../../tmp/a11-fs-sqlite-20261119.log) · [S3 等待现场](../../tmp/live-cli-startup-evwsgefe/editor-browser-failure.png) · [中间修订检查](../../tmp/a11-s3-sqlite-final-20261119.log)

## 复跑与回退

使用生产前端构建、Chromium、实际 API 和授权的 `deepseek-flash`。运行器在浏览器和存储断言通过后，把验收摘要写入隔离算例，由真实模型选择文件读取工具并原样返回摘要。`VIBEFLOW_TEST_WEB_DIST` 指向已构建前端，`VIBEFLOW_TEST_BROWSER_PYTHON` 指向装有 Playwright 的 Python。入口参数 `--source-profile-id 26`，可组合 `--s3` 和 `--postgres`。故障屏障只延迟真实存储操作。[专项入口](../server/live_llm_editor_conflict_acceptance.py)

本次无数据库迁移。需要回退时可恢复上一版前端和读取处理代码，既有文件、修订和条件写入接口仍兼容。旧客户端省略条件字段的写入由历史 API 兼容路径处理。[前端构建](../../tmp/a11-editor-build-20261119.log) · [界面检测](../../tmp/a11-impeccable-detect-20261119.log)
