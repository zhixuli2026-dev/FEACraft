# 镜像清理业务入口的共享截止时间

2026-11-07，接续[原生调用共享期限](native-call-shared-deadline.md)。把镜像清理服务入口开始的归档、准入、准备、执行和提交纳入同一个默认 300 秒单调截止时间。认证依赖、入站解析和其他业务入口仍列为后续范围，不将本入口的覆盖扩张成全站 HTTP 保证。

## 实现

- 新增 `request_deadline.Deadline` 与上下文：嵌套调用只取更早期限，ContextVar 随实际同步线程传递。外层期限触发取消，已有 `finish_before_cancel` 继续等待已持有的线程、预留或提交结束；期限不授权丢弃它们。
- 原生客户端、服务端执行器和 `run_sync` 继承业务期限，并继续与自身及上级执行预算取最早值。节点存储准入的锁和扫描也使用更早期限，不能因切换到线程而获得新的测量时间。
- 首次恢复归档校验移到数据库写锁之外。随后重新获取实际 case 控制锁，再校验相同退休凭证和快照选择、重新检查权限及读者，才允许写清理意图。最终原生放行前仍重新校验归档。
- 已分配节点额度先保留实际回执，配置写入和创建 owner 都先等待实际完成。若 owner 已提交、原生执行尚未开始时超时，明确将该空执行记录终结为 failed / TimeoutError，再按实际排空证据释放额度。
- 到期后不开始新的业务阶段。最终提交进入受保护操作后可以在期限外完成：若调用者已超时，返回未完成响应，但随后查询和重试必须读出唯一已提交事实，不能重写成功记录或重复删除。

## 实际验证方法

显式入口：`live_llm_image_request_deadline_acceptance.py --source-profile-id 26 [--postgres] [--stage ...]`。全部使用实际 API、数据库事务、文件和 Windows/WSL 进程。非模型阶段不伪造执行回执；最终通过授权的真实 DeepSeek 选择工具读回恢复文件。

| 位置 | 验证的实际行为 |
| --- | --- |
| db_lock | 另一事务持有真实写锁，期限结束后取消等待；解锁后不能迟到写入意图或启动执行 |
| archive | 实际归档校验结束但线程回调未返回，期限到达后仍等待线程；期间竞争数据库写入可获得锁 |
| admission | 实际额度文件已经创建，回调迟到时保留该文件，排空后才释放 |
| settings | 实际配置文件已经写入，等待线程结束后拒绝继续创建 owner |
| owner_commit | 实际 owner 已提交而调用者尚未拿到返回；排空后将无子进程记录明确终结 |
| release_archive | 原生进程已登记，放行前归档线程延迟；业务期限传入原生调用，到期停止、实际核验退出，镜像保留 |
| before_commit | 原生删除已完成，完成事件尚未提交；期限取消事务，保留原生事实和快照，后续可处理已不存在的镜像 |
| after_commit | 完成事件已实际提交但提交回调尚未返回；查询已显示 completed，原请求排空后返回 409，重试返回同一结果 |

前五处使用 8 秒算例预算，原生放行和最终提交处使用 30 秒。SQLite 的首轮写锁由 Case 行更新取得，覆盖 SQLite 实际全库写锁；后续明确使用 CaseEventCounter，PostgreSQL 同时验证实际 case 控制行争用。没有用 SQL 假记录代替进程出生或停止。

## 本批证据

SQLite 首轮七位置（不含 release_archive）全部通过：[矩阵](../../tmp/sandbox-recovery-ui-retirement-3ietssc8/http-request-deadline-checks.json)、[真实模型](../../tmp/sandbox-recovery-ui-retirement-3ietssc8/live-result.json)。8 项基础设施与 1 项模型验收，真实调用 2 次、8,354 tokens，严格读回通过。

SQLite 原生放行前单独补验通过：[矩阵](../../tmp/sandbox-recovery-ui-retirement-ti6fh6lt/http-request-deadline-checks.json)、[真实模型](../../tmp/sandbox-recovery-ui-retirement-ti6fh6lt/live-result.json)。2 项基础设施与 1 项模型验收，真实调用 2 次、8,448 tokens。实际请求 failed / TimeoutError、子进程停止原因 timeout 且 verified；最终清理成功，严格读回通过。

PostgreSQL 完整八位置通过：[矩阵](../../tmp/sandbox-recovery-ui-retirement-44h1iyfp/http-request-deadline-checks.json)、[真实模型](../../tmp/sandbox-recovery-ui-retirement-44h1iyfp/live-result.json)、[临时库删除](../../tmp/image-postgres-lxcngjzq/result.json)。9 项基础设施与 1 项模型验收，真实模型 2 次、8,424 tokens，严格读回通过。PostgreSQL 的真实 CaseEventCounter 行锁争用及归档暂停时的竞争写入均通过。

以上两套 SQLite 验收的生产代码相同，测试边界扩展单独记录，不把首轮称为八位置。三套合计 19 项本批实际基础设施检查、3 项真实模型验收；真实 `api.deepseek.com / deepseek-flash` 推理 6 次，全部 HTTP 200，25,226 tokens，模拟 0。35 个登记进程出生全部核验退出。原生放行前超时保留镜像；提交前超时保留已删除的实际事实；提交后超时的重试返回唯一已提交结果。基础退休算例的权限/损坏注入仍单独视为协议检查，不重复计入本矩阵。历史模拟集合未运行，审查限制未放宽。

## 继续范围

默认 300 秒是允许开始和推进业务的截止时间，不是清理必须瞬间完成的承诺。已提交写入、不可中断线程和实际进程退出核验仍需等待真实完成；本批未证明操作系统挂死时能硬性终止所有 I/O。

继续认证/入站等待、其他 native request / CLI / 文件入口的预算、同步 sink 与线程池/队列的有界性、全局并发额度。此前偶发退出核验拒绝和宿主时延、原生后续 330 秒期限、WSL/宿主重启及真实空间/权限故障仍未标记修复。主线 33/53、剩 20；资源 11/17、剩 6；全部 295 项最终交付目标保持。
