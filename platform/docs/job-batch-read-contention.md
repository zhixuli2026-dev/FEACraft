# 作业批次读取与 SQLite 写锁

2026-11-19，N4-05 / RB-14。几何浏览器验收中，作业面板的 `GET /cases/{case_id}/job-batches` 执行 case 事件计数器的 `INSERT ... ON CONFLICT DO NOTHING` 时遇到 SQLite 写锁，返回 500。使用真实 HTTP 和同库另一 SQLite 连接持有 `BEGIN IMMEDIATE` 后，独立复现相同失败点。基线夹具将 SQLite 驱动等待缩短至 100 ms，生产驱动配置未改。该问题和几何脚本提交后自动预览的配额错误有各自的调用路径。[浏览器原日志](../../tmp/geometry-tolerance-browser-8nxgi12j/api.log) · [基线结果](../../tmp/job-batch-read-contention-38xj1rrb/job-batch-read-contention.json) · [基线 SQL 栈](../../tmp/job-batch-read-contention-38xj1rrb/api.log)

SQLite 加普通文件存储的列表和详情先验证权限，然后在新事务中通过 `BEGIN IMMEDIATE` 申请写者。只有在准入阶段遇到 SQLite `BUSY` 或 `LOCKED` 才重试，每次等待前归还连接。两路沿用文件读取共享期限，超期返回 `504 job_batches_read_timeout`。清单和索引继续在 case 上的文件事务恢复护栏内读取。读取清单后在写者仍持有时建立 WAL 只读快照，再提交短事务、释放写者并读取 Job、Attempt 与已有批次投影。普通文件编辑、批次提交与取消沿用各自的事务。PostgreSQL 和 S3 的读取沿用其原准入路径。[路由实现](../server/app/routers/jobs.py)

最终受审套件 1/1 通过。短时持锁释放后列表用时 1.047 秒返回 200，详情用时 0.688 秒返回 200。持续持锁时，在夹具配置的 3 秒期限后用时 3.235 秒返回 504，锁释放后再次读取成功。实际 Windows 批次命令完成并在作业日志读到预期 stdout。状态投影暂停期间，独立 SQLite 连接成功取得写者。跨用户列表与详情均返回 404，隔离 API 的实际出生身份已核验退出。[通过回执](../../tmp/job-batch-read-contention-f03vpe02/job-batch-read-contention.json) · [执行器摘要](../../tmp/vibeflow-regression-all-20261119-130805-21102c/summary.json) · [基线执行器摘要](../../tmp/vibeflow-regression-all-20261119-130454-bc92d9/summary.json)

新旧接口共用原批次投影。列表为数组，空列表是 `[]`，详情为单对象。原投影字段为 `id`、`case_id`、`created_at`、`status`、`max_concurrent`、`total`、`active`、`counts`、`jobs`，已登记专业批次可附 `operations`。本轮实际命令批次在同一终态下的列表单项和详情响应整体相同。专业批次沿用原绑定校验，其内容不在本次实际持锁矩阵内。[批次返回结构](../server/app/routers/jobs.py) · [验收入口](../server/job_batch_read_contention_smoke.py)

本轮验证 Windows 上的 SQLite 与普通文件存储。其余 RB-14 长 I/O 和事务等待按原任务推进。真实与模拟模型调用均为 0 次。C 盘可用空间 22.19→22.20 GiB，隔离数据和回执位于 D 盘，未操作 Docker。

作业收尾加入有界写锁准入后，使用当前组合源码复跑批次专用验收，1/1 通过。[组合复验](../../tmp/vibeflow-regression-all-20261119-132533-48dbca/summary.json)
