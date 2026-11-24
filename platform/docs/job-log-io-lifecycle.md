# 作业日志读取的并发、期限与连接释放

2026-11-17，N4-05 / RB-14 的具体进展。主线仍为 49/53、剩 4 项；资源清单仍为 11/17、剩 6 块。本记录不代表全部日志兼容性、全部长 I/O 或跨节点并发已完成。

## 实现

- `job_log_reader.select` 在已授权的数据库读取阶段复制必要字段，包括作业/尝试身份、文件位置、公开状态及停止是否已核验。工作线程只接收不可变选择，不接收 SQLAlchemy 对象或数据库会话。
- 普通日志 GET、作业详情的冷读取和流式分页在访问文件/S3 前结束读取事务。游标流每次重新核验账户、case 和尝试身份，关闭短会话后才读存储。
- `job_log_io` 使用独立的 4 个工作线程和非等待准入；第 5 个并发读取立即返回 429 `log_read_busy`。这是一份 API 进程内的限制，未宣称跨 worker/节点额度。
- 每次读取沿用现有 `request_deadline` 和 `finish_before_cancel`，预算 30 秒。开始新 I/O 前及返回成功前检查期限；取消/超时后的已开始同步 I/O 继续排空，期间不释放名额。因而实际响应可以晚于 30 秒，不能将这个实现描述为可强杀同步 I/O 的硬截止时间。排空后超时返回 504 `log_read_timeout`，响应体实际关闭。
- 旧版与带 offset 的 SSE 使用同一分页路径，每页 64 KiB，不再为浏览器创建 Job 输出订阅队列。已有本地 `completion_error` 仍返回 `persistence_failed`，不会将收尾失败发成成功 done。
- 无本地 owner 的作业详情仅读取末尾 4,000 字节，保留完整 UTF-8 字符。无 limit 的普通日志 GET 继续支持完整读取。

## 真实 S3 与实际阻塞测试

最终入口：`job_log_range_capacity_acceptance.py --require-ranges --require-lifecycle`；审查：`tmp/N4-05-job-log-lifecycle-review-v2-20261117.json`。实际记录：`tmp/job-log-range-capacity-ic_q14kr/range-result.json`。

真实 Windows Python 作业先输出一段中文/emoji，再等待 3 秒输出第二段；运行期间旧 SSE 收到 running 数据，完成后收到 done，最终 2,300,041 字节与真实日志逐字节相等。API 正常关闭重启后，6 组分页、旧完整 GET、两种 SSE 和 4,000 字节详情尾部通过。所有 S3 分页正文均为实际 If-Match/Range 请求并关闭响应体。

在隔离 API 包装真实 S3 响应体的 `read`，以文件开关暂停真实读取，不替换内容、HTTP 结果或进程：

- 同时保持 1 条 SSE 和 3 个 GET 的实际响应体，观测到 4 个读取，数据库池已释放，健康检查在 1 秒内响应。
- 第 5 个请求返回 429；实际断开 SSE 后，其线程尚未排空，继续请求仍为 429，不能通过断连提前回收名额。
- 释放文件开关后，3 个 GET 返回完整正确页面，4 个响应体均关闭；随后读取恢复为 200。
- 另一个读取保持超过生产 30 秒预算。在释放前请求仍等待真实 I/O 排空，数据库连接不被占用；释放后返回 504，再次读取恢复为 200。

这是实际基础设施加受控 I/O 延迟测试，不是实际对象存储自行发生故障，也不等同于任意网络或永久阻塞组合。

## 本地文件与实际取消

最终入口：`job_log_fs_lifecycle_acceptance.py`；审查：`tmp/N4-05-job-log-fs-lifecycle-review-v3-20261117.json`。实际记录：`tmp/job-log-fs-lifecycle-i4x2amz9/fs-log-result.json`。

两份真实作业分别正常完成和经实际取消接口结束。运行时旧 SSE 持续输出；最终输出与文件逐字节相等，停止凭据均 verified，未创建浏览器订阅队列。正常关闭 API 并重启后，两份作业的分页、完整 GET、4,000 字节尾部和两种 SSE 均通过；实际 SQLAlchemy checkout/checkin 按请求路径观测，日志请求没有遗留连接。最终 S3/FS 共 3 份作业、4 次实际 API 出生身份退出；S3 隔离服务已停止。模型调用 0、模拟模型调用 0、Claude 调用 0。

保留的中间记录：`tmp/job-log-range-capacity-rfumlkau` 是首版 S3 通过结果及 646 份源码。`tmp/job-log-fs-lifecycle-qufize71` 中实际取消成功，但测试等待函数误用默认 succeeded；原数据库已确认两份作业停止 verified。`tmp/job-log-fs-lifecycle-ocl_am_d` 的重启读回已进行，最后的聚合连接池断言失败；聚合池不能区分独立后台任务，最终增加真实连接归属观测并保留聚合值。两次 FS 失败及各自 647 份源码、实际 API 退出记录均保留，不用最终通过覆盖失败。

## 尚未收口

旧本地 SSE 的内存诊断兼容已补齐：封存前诊断有界写入日志，封存后收尾错误通过旧 SSE 的独立诊断事件传递，两种流均返回 `persistence_failed`，不修改原始字节游标或封存摘要。FS/S3 的真实超时、输出超限及受控收尾失败已验证，S3 失败归档恢复及同一算例后续执行也已通过，详见 [诊断与失败归档恢复](job-log-diagnostics.md)。这覆盖上述实际场景，不代表所有错误组合；晚到内存诊断尚不提供跨 API 重启重放。

每个读取的 30 秒预算尚不等同于路由正文、依赖、鉴权和响应发送的整请求预算；慢客户端发送、永久阻塞、跨 worker/节点额度及其他原生/CLI 入口仍按原 RB-12 至 RB-17 推进。旧本地 `JobProcess.backlog()` 本身的内存复制也未在本轮改写。全功能 295 项不因本轮局部检查批量提级。
