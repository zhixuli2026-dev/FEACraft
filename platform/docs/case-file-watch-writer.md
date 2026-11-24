# SQLite 文件监听空闲扫描的写锁

2026-11-19，N4-05 / RB-14。旧 `/ws/events` 在每轮文件扫描前都会取得算例锁。配置为 SQLite 时，这个锁占用数据库写者，哪怕该轮文件没有变化。实际 WebSocket 启动并保存基线后，夹具在真实文件系统遍历已读取第一项时暂停。第二个 SQLite 连接执行 `BEGIN IMMEDIATE` 返回 `SQLITE_BUSY`，另一算例的公开条件保存返回 409 `storage_quota_busy`。原失败已保留，真实隔离 API 已退出。[基线回执](../../tmp/case-file-watch-writer-2sici13d/case-file-watch-writer.json) · [执行器摘要](../../tmp/vibeflow-regression-all-20261119-134426-40de22/summary.json) · [API 退出观察](../../tmp/case-file-watch-writer-2sici13d/baseline-api-exit.json)

SQLite 与普通文件存储上的已知基线，先在短只读事务中检查算例和存储状态，退出事务后复用有界文件读取扫描。初步检查沿用轮询读取对持久存储租约、待恢复操作和已准入或正在初始化的 CLI 工作副本的判定。未发现文件变化，且扫描前后均无未完成的文件或目录事务时，本轮结束。初次扫描、摘要差异、无法核验或待恢复的情况继续进入原算例协调器。协调器重新读取真实文件与数据库基线，以同一个事务提交实际变化和观察记录。同一算例在多个 API 上独立监听时，只有协调后的观察能生成事件。S3 和 PostgreSQL 走原读取路径。[实现](../server/app/services/case_file_watch.py) · [轮询读取准备](../server/app/services/case_read_capacity.py) · [原文件监听接口](FF-H09-external-file-watch-acceptance.md)

最终受审套件 1/1 通过。监听在同一文件遍历位置暂停时，独立 SQLite 写者成功取得锁，另一算例的公开条件保存返回 200。物理文件的 SHA-256 与响应一致。两个独立 API 使用同一 SQLite 和文件目录，分别认证并订阅同一算例。外部真实新增只产生一条持久观察事件，两边收到同一事件 ID 和序号。文件内容保持不变时，真实未提交的文件事务仍恢复为 `rolled_back`，没有新增观察事件。断开连接后监视结束，两台隔离 API 的实际出生身份均核验退出。[最终验收](../../tmp/case-file-watch-writer-v819r25z/case-file-watch-writer.json) · [执行器摘要](../../tmp/vibeflow-regression-all-20261119-134742-df1eb9/summary.json)

本轮覆盖 SQLite 与普通文件存储的空闲监听。真实及模拟模型调用均为 0 次。隔离文件位于 D 盘，C 盘可用空间从 22.21 GiB 变为 22.20 GiB。未操作 Docker。
