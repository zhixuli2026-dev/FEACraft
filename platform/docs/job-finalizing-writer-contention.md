# SQLite 作业收尾的写者准入

2026-11-19，N4-05 / RB-14。B05 浏览器运行的手动 CadQuery 作业已经生成私有场景，Windows 守护回执记录 6 个成员全部退出。数据库仍停在 `running`，新的发布尚处于 `prepared`，公开场景保留上一次提交。旧日志同时出现批次 GET 504，能够缩小到收尾写入的前置窗口，现有记录无法识别当时实际持锁的连接。[B05 浏览器记录](../../tmp/geometry-autopreview-handoff-browser-1r80__g9/geometry-handoff-result.json) · [B05 API 日志](../../tmp/geometry-autopreview-handoff-browser-1r80__g9/api.log)

定点试验使用公开手动预览接口启动一个真实 CadQuery 作业。其根进程以 0 退出，守护回执已验证退出且无活动成员。夹具随后在进入 `phase(finalizing)` 前让独立 SQLite 连接持有 `BEGIN IMMEDIATE` 0.8 秒。隔离连接的驱动等待设为 100 ms。旧代码在约 0.156 秒后执行事件计数器 `INSERT ... ON CONFLICT DO NOTHING` 时抛出 `OperationalError`，SQLite 原始错误为 `SQLITE_BUSY`（5）。锁释放后，原 attempt 仍为 `running`，无已提交的 `finalizing` 事件、输出清单或作业停止回执。随后以实际出生身份核验该隔离 API、作业宿主和守护进程全部退出。这个定点结果证明首条状态写入缺少等待，对 B05 自然运行中的持锁方仍保留未知。[精确异常与阶段轨迹](../../tmp/job-finalizing-writer-contention-uz58_lx3/finalizing-trace.jsonl) · [基线验收](../../tmp/job-finalizing-writer-contention-uz58_lx3/job-finalizing-writer-contention.json) · [进程退出观察](../../tmp/job-finalizing-writer-contention-uz58_lx3/baseline-exits.json)

SQLite 的 `phase(finalizing)` 现在先在干净事务中等待 SQLite 写者准入。准入复用已有存储结算期限和 SQLite 锁实现。写锁繁忙时归还连接再重试。取得写者后再读取原 Job 和 attempt 的所有权，继续原状态变化、事件和提交。重试只覆盖准入，原生执行、发布和已开始的业务写入不重放。基线夹具发现 Windows 的两个单调时钟采样可能相同，最终以已保存的事件顺序断言。夹具等待 CadQuery 输出的时间从 45 秒增至 65 秒，生产期限仍沿用原配置。[状态入口](../server/app/services/job_execution.py) · [准入实现](../server/app/services/storage_admission.py)

首次修复后复跑在锁释放后进入 `finalizing`，并提交停止回执。该轮夹具给所有 SQLite 连接都设了 100 ms。作业心跳同轮报告 `OperationalError`，待收尾作业最终以 `persistence_failed` 结束，因此保留整体失败结果。最终夹具只将 `phase(finalizing)` 自己签出的连接设为 100 ms，并在轨迹记录实际 PRAGMA 值。心跳和其他正常数据库工作使用生产的 30 秒驱动等待。[首轮修复轨迹](../../tmp/job-finalizing-writer-contention-z7wiv25p/finalizing-trace.jsonl) · [保留失败](../../tmp/job-finalizing-writer-contention-z7wiv25p/job-finalizing-writer-contention.json)

最终受审套件 1/1 通过。在真实输出生成、根退出码为 0、守护确认 6 个成员退出后，另一连接实际持锁约 0.813 秒。锁释放后约 0.031 秒，同一次 attempt 持久进入 `finalizing`，随后以退出码 0 完成。数据库记录一次原生启动、一次 `artifact.published` 和一次 `attempt.completion_visible`，保存已核验的停止回执。公开接口读取到同一提交的 publication `9385c06b-f9d4-4aae-9916-b0b0cf16bf23` 与真实 `sqlite_finalizing_writer` 场景。隔离 API 出生身份已核验退出。[最终阶段轨迹](../../tmp/job-finalizing-writer-contention-3gof1m1t/finalizing-trace.jsonl) · [最终验收](../../tmp/job-finalizing-writer-contention-3gof1m1t/job-finalizing-writer-contention.json) · [执行器摘要](../../tmp/vibeflow-regression-all-20261119-132208-af895d/summary.json)

本轮覆盖 Windows、SQLite 和普通文件存储中的收尾准入。真实与模拟模型调用均为 0 次。隔离文件位于 D 盘，C 盘可用空间从 21.88 GiB 变为 22.07 GiB，期间最低观察到 20.37 GiB。未操作 Docker。
