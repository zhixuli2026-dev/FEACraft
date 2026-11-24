# FF-H07 工作区全文搜索验收

2026-11-18，在独立 PostgreSQL 数据库上运行当前 API 和生产构建的前端，分别使用本地文件存储与 SeaweedFS。每轮创建三个算例，写入同路径、同关键词但正文不同的文件。两个算例建立索引，第三个保留未索引状态。[文件存储结果](../../tmp/live-cli-startup-_5fp6947/workspace-search.json) · [S3 结果](../../tmp/live-cli-startup-bvuv14o4/workspace-search.json)

| 用户操作或条件 | 实际结果 |
|---|---|
| 在浏览器建立索引和搜索 | 所有者从未建库的算例点击建库。在当前算例搜索得到一个命中，切换到整个工作区得到两个带算例名称的命中。点击第二项后，Monaco 打开第二个算例独有的正文。[搜索结果](../../tmp/live-cli-startup-_5fp6947/workspace-search.png) · [打开目标文件](../../tmp/live-cli-startup-bvuv14o4/opened-target.png) |
| 文件名、正文、范围和限额 | 两个并发 HTTP 搜索得到一致的路径、所属算例和排序，已索引数为 2、算例总数为 3。只搜文件名时，正文关键词没有命中。同路径文件名在两个已索引算例中都能找到，限制为 1 时只返回一个。空白、超长查询和非法条数均被拒绝。[验收入口](../server/live_llm_workspace_search_acceptance.py) |
| 授权及撤销 | 匿名请求返回 401，其他组织的用户返回 404。接受组织邀请并启用工作区共享后，只读成员能搜工作区并查看索引状态，重建返回 404。撤销共享后，工作区和算例两级搜索均返回 404。[双后端权限记录](../../tmp/live-cli-startup-bvuv14o4/workspace-search.json) |
| 索引兼容、故障与恢复 | 旧版索引原位打开后仍使用原构建标识。新建索引的文件行、FTS 行及指纹与旧实现逐项一致。构建写入故障后可完整读回上一次结果，解除故障后同长度内容变更可查，原事件收据仍有效。另实测活动锁排斥、过期锁接管、进程退出后的事件重投。[资源测量与重启](resource-budget-worklist.md#2026-11-18-全文索引重建) · [文件存储兼容](../../tmp/index-streaming-bp2985jd/result.json) · [S3 兼容](../../tmp/index-streaming-ufcsa55u/result.json) · [锁检查](../../tmp/h07-index-lock-20261118/summary.json) |

首次 S3 浏览器及权限检查通过，随后模型回读在发送前遇到 409。在专门的并发复现中，记录到同一消息请求确实遇到了存储额度占用。修复将有限等待放在写入用户消息之前。最终双后端各通过两次真实 DeepSeek 请求，完成工具读取和回答；S3 在确认发生相同竞争后返回 200，数据库只记录一条用户消息。[修复前](../../tmp/live-cli-startup-t4ir43pd/search-admission-status.json) · [修复后](../../tmp/live-cli-startup-bvuv14o4/search-admission-status.json) · [文件存储模型回读](../../tmp/live-cli-startup-_5fp6947/live-result.json) · [S3 模型回读](../../tmp/live-cli-startup-bvuv14o4/live-result.json)
