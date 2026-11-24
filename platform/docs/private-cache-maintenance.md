# 私有缓存分页维护与退出恢复

N4-05 / RB-15 的启动和定期维护现在每轮最多检查 32 个本地缓存候选，交替读取 `.attempt-sources` 和 `.case-read-views`。本轮结束时保留目录游标，在后续周期接续。每轮缓存页后继续处理账户维护，停机先等待已开始的工作线程归还，再关闭目录句柄。后台扫描的期限使用已有存储测量配置。[缓存恢复](../server/app/services/private_cache.py) · [后台调度](../server/app/services/storage_maintenance.py)

缓存处理沿用原进程出生身份、文件大小、SHA-256 和目录清单。逐条读取 JSONL、分块计算摘要，删除前完整检查实际目录与记录，删除元数据前再核对已打开的文件。到期后停止后续校验，保留未清理内容和必要的证明，下一轮重新核验。已启动的同步文件读取需先返回。前台由当前所有者完成的正常清理沿用其原处理路径和证据要求，后台条目上限仅用于后台清扫。无数据库迁移，旧的缓存清单格式仍适用。

## 实际验收

专项把页大小设为 2、读取预算缩为 0.4 秒、后台单缓存清单上限设为 8。先由真实 API 和独立进程写缓存，终止原 API 后启动新的维护进程。每个数据库与存储组合都观察到两个根目录交替、缓存页之间的账户维护、慢读超时后重试、活跃进程的缓存保留及该进程退出后的清理。损坏内容、未登记文件、残缺日志、过大清单和 6 个无凭据的旧目录保持原字节。前台按原证明清理了 10 文件缓存，停止后台时所有打开的目录游标均关闭。[专项入口](../server/live_llm_private_cache_maintenance_acceptance.py)

| 数据库与存储 | 私有缓存分页与状态 | 模型回执 |
| --- | --- | --- |
| SQLite 与文件系统 | [运行记录](../../tmp/live-cli-startup-nd7vu5sm/private-cache-maintenance.json) | [回执](../../tmp/live-cli-startup-nd7vu5sm/live-result.json) |
| PostgreSQL 与文件系统 | [运行记录](../../tmp/live-cli-startup-uok4kovx/private-cache-maintenance.json) | [回执](../../tmp/live-cli-startup-uok4kovx/live-result.json) |
| SQLite 与 SeaweedFS | [运行记录](../../tmp/live-cli-startup-694ivlm0/private-cache-maintenance.json) | [回执](../../tmp/live-cli-startup-694ivlm0/live-result.json) |
| PostgreSQL 与 SeaweedFS | [运行记录](../../tmp/live-cli-startup-bh612sxd/private-cache-maintenance.json) | [回执](../../tmp/live-cli-startup-bh612sxd/live-result.json) |

S3 封存输入重试也在两种数据库通过。四个真实 Python 作业验证了原封存输入复用、损坏输入拒绝、旧文件保持和独立读取缓存回收。最终维护专项 8 次、封存输入回归 4 次、早期维护专项 8 次，阶段共有 20 次真实 `deepseek-flash` HTTP 200，模拟调用 0。模型通过文件工具读取验收摘要。[SQLite 重试](../../tmp/live-cli-startup-3cqqhvct/archived-input-retry.json) · [PostgreSQL 重试](../../tmp/live-cli-startup-cuhsizg3/archived-input-retry.json)

首次加入停止检查时，隔离接口缺少 `asyncio` 导入，四轮在停止接口返回 500。原日志已显示目录句柄成功关闭。修正验收夹具后，四种组合完整通过，失败轮次未执行模型。[停止夹具失败记录](../../tmp/live-cli-startup-t_lhgy9d/api.log)

RB-15 的旧预留历史压缩与结果未明的记录处置继续按原清单跟踪。需要回退可恢复上版调度和缓存清理代码，已有记录的格式仍兼容。[资源预算工作清单](resource-budget-worklist.md)
