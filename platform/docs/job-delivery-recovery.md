# 持久投递意图与排队任务恢复

状态：本批实现与隔离验收完成，N5-01/02/03已按原条件收口。主线14完成、16进行中、23待开始，剩39项；295项全功能仍按完整条件验收。

原流程在提交 Job 后直接启动或发送 Redis 消息。API 在提交与启动之间退出，或 Redis 丢失消息后，没有可重复恢复的投递环节。现在普通单任务、批次成员及已注册的专业 handler 在提交事务内保存 `attempt.delivery_requested` 与 outbox。事件绑定 case、Job、attempt、请求用户、执行规格摘要、原运行主机、local/redis 模式，以及批次成员和并发上限。没有新增表或迁移。

恢复器只扫描持有明确意图且尚未认领的任务。每批最多读取 64 项，最多同时准备 4 项，按 Job ID 轮转，避免前一页长期待处理的任务阻塞后续页面。反复投递使用原 Job 和 attempt；真正运行仍须通过既有事务内排他 claim，提交 claim 后才准备文件和创建原生进程。已认领、已取消、已结束或只有旧 queued 记录的任务不会被自动重放。

实际 claim 会重新校验执行规格、原主机、请求者写入权限、case 可写状态、工作流活动尝试与状态。批次成员须保存一致的意图。case 控制锁之后取得 workspace 所有者的 User 行锁，在不取得其他 case 控制锁的情况下计数：所有现代投递 claim 在该用户锁上串行，避免不同 case 同时越过最后一个名额。已认领的准备中、运行中、收尾中或缺少清理证明的任务仍占容量；有完整停止及写入排空证明的终态才释放。批次容量同样在 claim 内判断，不依赖已失去的内存 BatchState。

工作流投递只恢复已经完成准备和明确排队的步骤，不跳过依赖、确认门或自动创建新的步骤。专业 handler 的不可变输入、调用身份、发布准备和收尾协议继续由原实现验证。旧直接入口没有持久执行规格的路径仍需接入，不能从 Job.command 猜测 argv、运行环境或回调。

Redis 模式保持 Redis 投递，不回退为 API 本地执行。消息使用 attempt 的确定性标识，重复扫描不堆积重复消息；消息内容只携带 Job ID，worker 从数据库读取并核验意图。Redis 执行结果不作为任务真相；传输结果到期、丢失或发送确认异常后，尚未 claim 的意图仍可再次投递。当前运行主机约束也适用于 Redis worker，跨主机工作目录映射与接管未在本批交付。

Windows worker 使用 `python -m app.services.distributed_worker`。该入口适配 arq 的信号注册及其关闭时对 SIGUSR1 的假设；Linux 保留 arq 原行为。这里处理启动/关闭兼容，完整的优雅停机、紧急停机分类和看门狗仍按 N4-07 验收。

## 验收与明确边界

- `job_delivery_recovery_smoke.py --postgres --wsl`：真实 API 在提交后、claim 前退出，两个新 API 恢复 Windows/WSL 载荷；原 attempt 只启动一次。另覆盖批次上限、用户额度、排队取消、跨 case 同一用户并发认领、回滚、损坏规格及外部主机拒绝。
- `job_delivery_redis_smoke.py --postgres --redis-root <目录>`：独立 WSL Redis 与真实 arq worker。停止原 API，实际停止并重建无持久化 Redis 使队列清空，确认新 API 从 SQL 重建单条消息并执行原 attempt。Redis 用 OwnedProcess 管理，保存真实停止证明。运行时依赖为 Ubuntu 仓库中的 redis-server/redis-tools 与共享库，可在测试目录解包，不需安装或修改用户 Redis 服务。
- 旧 `workflow_handler_smoke` 的“晚到旧代数”测试在本批修改前的验收归档上同样出现原生测试门超时。旧脚本等待新代数发布完成才释放旧写者，与已存在的发布前排空约束相冲突。修正为等待新代数进入 finalizing，再释放旧写者；旧代数 superseded、当前指针和确认拒绝断言保持不变。

未接入的旧直接启动入口、旧记录人工恢复、跨主机调度、完整工作流续跑、整体预算/公平/停机和 finalizing 文件补交仍需完成。本批不将投递恢复视为整个 N5 或全部功能交付。

开发数据库未迁移，端口 8000 未重载。最初误直接运行旧批量脚本时，其默认 8000 连接被拒绝，没有成功访问服务；随后改用 execution profile 的临时 API 验收。正式 OpenAI 登录联调继续按用户决定暂缓。


规范证据：`tmp/mainline-N5-delivery-evidence/`。当前源码959文件/Python575；app251/路由227/边界351；注册all279/offline106/writes30。head6a4fc1b5e087/36业务表不变。
