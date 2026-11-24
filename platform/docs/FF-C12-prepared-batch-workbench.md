# FF-C12 多工况批次

在「作业 → 新建任务 → 批次 → 多工况求解」里选择当前算例已有的 `C-run` 子目录。每个工况可选择串行、MPI 或 UDF，配置对应的求解器、子域数或函数名。批次接受 1–50 项，并发上限为 1–4。提交前展示每个成员的目录、方式和资源上限。提交后的批次详情按原作业号显示工况目录，成员可打开各自的日志和恢复入口。

多工况批次复用 `POST /cases/{id}/jobs:batch` 的注册 `operations` 请求与按原键查回执的入口。服务端仍核对目录归属、独立目录、用户配额与执行绑定。旧「命令列表」仍使用同一批次入口的 `commands` 请求，支持原有运行环境。跨算例的类型工作流批次有独立 API；原 C12 要求的是同算例内多个派生工况。200 作业压力与多 worker 通用调度分别由 FF-J21 和 FF-J14 跟踪。

## 验收

| 项目 | 实际结果 | 证据 |
| --- | --- | --- |
| 浏览器工作台 | Edge 连接隔离 SQLite/文件系统 API。用真实 WSL `blockMesh` 准备两个工况。重复目录在请求发出前拒绝。已接收的响应被测试网络层截断后，刷新查回同一批次。批次页显示两个原目录，先呈现「正在取消」，两个成员的持久尝试变为已取消后呈现「已取消」。620 px 宽度无横向溢出。另一轮复核了存储忙 409 后沿用原请求再次提交。 | [最终浏览器结果](../../tmp/ff-c12-prepared-browser-_k2lmmbt/browser.json)、[窄屏核对](../../tmp/ff-c12-prepared-browser-_k2lmmbt/compact-review.png)、[同键 409 重试](../../tmp/ff-c12-prepared-browser-9rtjt2pi/submitted.json) |
| 本机实际注册执行 | 文件系统及认证 S3 上三个注册模式真实运行，400 单元 `0.5` 时刻结果核对。三种运行中取消、排队成员未领取、旧成果保留、异常退出后历史回读和明确重跑。 | [本地批次验收](local-prepared-batch-delivery.md)、[原条件核对](N3-07-original-acceptance-audit.md) |
| 队列、权限与恢复 | 实际 Redis 与两个 worker 上交叉核对用户及批次上限，确认跨用户隔离、取消、进程退出、同键幂等和恢复后重跑。 | [同算例运行取消](same-case-batch-cancel-recovery.md)、[调度条件与隔离](N3-07-original-acceptance-audit.md#原条件逐项核对) |
| 旧命令批次 | 最新 UI 在同算例的专业成员确认停止后提交普通 Windows 命令，实际成功且日志有预期 stdout。既有入口分别验证了两个实际进程、用户并发限制和同键重试归属。 | [Windows 批次结果](../../tmp/ff-c12-prepared-browser-_k2lmmbt/browser.json)、[旧入口并发验收](local-prepared-batch-delivery.md#验收方法与当前证据) |

2026-11-20，当前版本在 PostgreSQL、认证 SeaweedFS 和实际非特权 WSL 上复验了三模式 400 单元结果、分别运行中取消、其余成员从未被领取和旧成果保持。随后客户端等待一次 HTTP 响应超时，当前轮未走完重启回读，收尾取消请求也有断言失败，临时 PostgreSQL 已删除。历史认证 S3、真实 API 退出及恢复通过记录见上表。[本次求解及取消](../../tmp/live-cli-startup-yzbpmn4f/local-prepared-progress.json) · [超时原始记录](../../tmp/live-cli-startup-yzbpmn4f/live-result.json) · [收尾结果](../../tmp/live-cli-startup-yzbpmn4f/local-prepared-cleanup.json)

Edge 联调使用 0 次模型调用。独立隔离算例汇总浏览器和原生结果摘要后，真实 `deepseek-flash` 使用平台工具读取并核对证据。两次 `chat/completions` 均返回 HTTP 200，严格回读通过。模拟模型 0 次。[真实回读](../../tmp/live-cli-startup-ms216lsf/live-result.json)

前端使用现有任务面板和会话原请求记录。变更可通过部署上一版前端构建回退。构建和验收在隔离目录进行。
