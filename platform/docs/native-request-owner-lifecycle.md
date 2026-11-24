# 通用原生请求的所有者创建与取消收尾

2026-11-07，接续资源预算 RB-13/14。修复 `native_request_execution.scope` 在创建所有者、构造执行器和停止心跳之间的责任空窗，适用于使用此公共作用域的原生请求。

## 实际问题与实现

原实现先等待 `records.create()` 提交并返回，再进入保护执行器的 `try/finally`。真实 SQLite 提交后、返回 owner 前连续取消请求，会留下 `phase=active`、没有子进程且没有结束时间的记录。修复前[复现记录](../../tmp/native-owner-lifecycle-77w_s2o_/native-lifecycle-checks.json)保留此事实；测试随后明确终结隔离库里的空记录，没有把修复前结果改成通过。首次测试曾因专用测试路由缺少 `/api/v1` 返回 404，保留在 `tmp/live-native-owner-reproduction-20261107.log`，不计为实现失败或验收成功。

现在创建 owner 位于整个作用域的异常处理内，并通过受保护协程将实际提交返回的 owner 写入作用域变量。即使外层反复取消，也先等待创建操作结束，再终结已取得的 owner；不会丢弃已经提交的返回值，也不会继续启动执行器。构造失败同样进入终结逻辑。已有 owner 在进入作用域时截止时间已到，也会登记失败，不放行原生命令；尚未创建 owner 时到期则不新增记录。

执行器启动之后和业务体返回之后再次检查继承的请求期限。关闭执行器后，心跳任务的取消与等待也受到保护，终态提交位于更内层的 `finally`，连续取消不能跳过提交。超时记录为 `failed / TimeoutError`，手动取消记录为 `cancelled / CancelledError`。停止进程和持久提交仍须实际完成，不能因为 HTTP 请求结束就视为已经收尾。

## 双库验收

显式入口：`platform/server/live_llm_native_lifecycle_acceptance.py --source-profile-id 26 [--postgres]`。`--reproduce` 仅用于保存修复前的基础设施失败，不调用模型，不应在修复后作为通过测试运行。

执行前检查了此入口、`native_request_lifecycle_checks.py`、`live_llm_native_shell_deadline_acceptance.py`、真实上游记录器 `live_llm_acceptance.py`、隔离 API 辅助代码和迁移函数。没有运行历史模拟回归集合，也没有更改回归审查登记来放行旧脚本。

SQLite [七场景记录](../../tmp/native-owner-lifecycle-d86xry22/native-lifecycle-checks.json)与 PostgreSQL [七场景记录](../../tmp/native-owner-lifecycle-p1ad0uoq/native-lifecycle-checks.json)均通过：

| 场景 | 实际证据 | 分类 |
| --- | --- | --- |
| 创建提交后连续取消三次 | 已提交记录保持活动直到释放暂停；随后取消终态，无子进程和后续启动 | 基础设施 |
| 创建提交后超过共享期限 | 等待实际创建返回后登记 TimeoutError，不启动执行器 | 基础设施 |
| 执行器构造失败 | 实际初始化后注入 OSError；已提交 owner 终结失败 | 协议/故障 |
| 实际 TCP 监听启动后取消 | 监听关闭，业务体未进入，owner 取消终结 | 基础设施 |
| 心跳收尾期间连续取消三次 | 先完成真实心跳 SQL 提交，再延迟取消收尾；外层等待，最终仍提交取消终态 | 基础设施 |
| 已过期、没有 owner | 不新增记录、不启动执行器 | 基础设施 |
| 已过期、已有提交的 owner | 原 owner 登记 TimeoutError，不启动执行器 | 基础设施 |

这些暂停包裹实际数据库操作或监听，不构造模型回复。两套矩阵合计 12 项基础设施、2 项协议/故障检查。

## 真实模型兼容

两套数据库分别通过真实 DeepSeek 自主调用 `run_shell_command`：PowerShell 实际执行 25 秒睡眠，工具总预算 12 秒，实际准备后额外暂停 5 秒。最终运行只获得剩余预算，超时后实际终止并核验出生身份退出，owner 以 TimeoutError 结束，模型接收实际工具失败并如实答复。

- [SQLite 模型记录](../../tmp/live-shell-deadline-oa0um3ur/live-result.json)：`api.deepseek.com / deepseek-flash`，2 次真实推理，9,708 tokens，子进程已退出。
- [PostgreSQL 模型记录](../../tmp/live-shell-deadline-1v7_vkkn/live-result.json)：同一服务和模型，2 次真实推理，9,831 tokens，子进程已退出。
- [PostgreSQL 收尾](../../tmp/native-lifecycle-postgres-p48zag3r/result.json)：实际临时数据库已删除，开发数据库未修改。两套模型和两套矩阵 API 均已退出。

本阶段合计 **4 次真实模型调用，全部 HTTP 200，19,539 tokens，模拟 LLM 0**。模型检查 4 项（两套各工具选择、失败续答），另外 4 项实际 Shell 期限/退出检查；加上双库矩阵，共 16 项基础设施、2 项协议/故障、4 项真实模型检查。

## 未完成范围与下一步

此次不新增数据库结构，不自动接管已有历史活动记录，也不把数据库提交等待变成可强制中断的系统操作。数据库故障或真实进程退出时的未知提交状态，仍不能凭取消或超时猜测为已完成。构造器当前不创建进程或监听；本次故障注入不证明任意未来构造副作用都能恢复。

其他入口的整体期限、原生 CLI 会话/turn/fork 生命周期、存储和工作目录外的强制限制、全局并发与等待队列、未知历史及副本维护继续。通用作用域改进不等于所有调用入口均已有完整资源预算。下一步优先检查 CLI 会话创建和访问准备中的额度、期限、取消与归属。

主线仍完成 33/53，剩余 20；资源完成 11/17，剩余 6。全部 295 项全功能交付目标保持，未以本轮局部检查代替整项终验。
