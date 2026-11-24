# FF-H08 文件和作业产物的搜索索引更新

2026-11-18，在 PostgreSQL 搭配文件存储及 SeaweedFS 的隔离环境中，验证了公开文件 API 的编辑和删除、真实 Windows 作业收尾和搜索索引更新。两组测试均未连接 WebSocket。[文件存储验收](../../tmp/live-cli-startup-gddg5avc/search-updates.json) · [S3 验收](../../tmp/live-cli-startup-53bagalh/search-updates.json)

| 验收内容 | 实际结果 |
|---|---|
| 保存、竞争写入和删除 | 暂停事件投递后，新增文件可以立即按正文找到。两个请求持同一原始 SHA-256 修改文件，只有一个成功，旧正文和被拒绝的正文均无命中。旧摘要删除返回冲突；持当前摘要删除成功，搜索随即移除结果。另一用户搜索、改写及删除均返回 404。 |
| 后台作业完成后索引更新 | 私有 shell 尝试的输出保留在所属尝试目录。独立的 convert 作业成功发布同路径、不同内容的文件。读取 SQLite FTS 证实，用户发起搜索前后台消费者已索引公开产物，搜索只命中当前可见路径。公开和私有两类行为遵循现有作业输入输出契约。[作业目录规则](default-attempt-workspaces.md) |
| 增量更新和手动重建 | 索引按内容摘要更新实际改变的 FTS 行，通过 rowid 移除已删除或超过全文大小上限的文件。手动全量重建保留。写入中断会整体回滚；旧版应用重建过同一索引后，新版会重新建立行号映射。校验仍扫描可索引文件。实测禁止改写一个无关文件的索引摘要时，其他文件更新仍成功。[文件存储](../../tmp/index-streaming-v8g63x0a/result.json) · [S3](../../tmp/index-streaming-696xc5et/result.json) · [锁竞争](../../tmp/h08-index-lock-20261118/summary.json) |
| S3 同秒覆盖 | 修复前，在文件大小和 LastModified 相同、ETag 已变化时，读取端仍可能认为索引有效，漏掉新正文。现在按对象版本核对；没有 ETag 时重新核验内容。保留了真实旧版失败和同一条件下的成功记录。[修复前](../../tmp/live-cli-startup-igs8vupk/same-second.json) · [修复后](../../tmp/live-cli-startup-53bagalh/same-second.json) |
| 提交后遇到本地存储额度锁 | 修复前，文件已保存但清理事务返回可重试的 409。现在在取得算例锁前等待账户额度锁；短暂竞争正常返回成功。若清理等待耗尽，返回 file_cleanup_pending 和 committed=true，刷新可以读到已保存的数据。SQLite 与 PostgreSQL 均用独立数据库连接占用真实锁复验。[原故障](../../tmp/live-cli-startup-xrqahsm2/post-commit.json) · [PostgreSQL](../../tmp/live-cli-startup-gddg5avc/settlement-timeout.json) · [SQLite](../../tmp/live-cli-startup-43jv1g2g/settlement-timeout.json) |

同版本浏览器补验覆盖搜索命中和打开正确文件，后台作业及并发编辑使用带身份认证的公开 API。[浏览器截图](../../tmp/live-cli-startup-dj5j3zm5/opened-target.png) · [专项入口](../server/live_llm_search_updates_acceptance.py)

与本阶段 V11、V17 一起，27 轮隔离联调共发出 46 次真实 DeepSeek 请求，模拟调用为零。其中 20 轮完整通过。7 轮失败记录均保留，6 轮在调用模型前即发现问题或遇到竞争，另一次恢复数据正确但模型额外调用了 list_files，未通过单工具读取断言。最终 V11、V17、FF-H08 双后端的六轮验收合计 12 次真实请求。增量索引版本另通过双后端的发布后进程中断与重复事件恢复。[模型超出工具次数的记录](../../tmp/live-cli-startup-3dhjykhj/live-result.json) · [V17 最终文件存储](../../tmp/live-cli-startup-bqdp7qo6/dictionary-crash.json) · [V17 最终 S3](../../tmp/live-cli-startup-lb9vawue/dictionary-crash.json) · [文件索引重投](../../tmp/live-cli-startup-yu4ko1rh/outbox-restart.json) · [S3 索引重投](../../tmp/live-cli-startup-qseal2ns/outbox-restart.json)
