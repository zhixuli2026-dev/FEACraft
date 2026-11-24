# CLI 会话启动的共享期限

2026-11-07，接续[通用原生 owner 收尾](native-request-owner-lifecycle.md)。幂等启动请求原已有 45 秒限制，直接创建会话则只依赖各准备/RPC 阶段自己的限制。现将会话服务启动统一为默认 45 秒，并继承调用者更早的截止时间。

## 实现与结果

`codex_sessions.Session.start` 从鉴权、CLI 发现、私有环境、程序摘要、staging、owner 提交到实际 stdio 初始化和激活提交，共用 `request_deadline`。`codex_session_requests.Execution.run` 使用同一个期限上下文，子会话不能重新获得完整 45 秒。业务体和受保护提交返回后均检查剩余时间，逾期不继续启动或返回活动会话。

CLI 发现、程序校验及启动回调中的文件核验改为等待实际线程结束再传播取消，避免请求已退出而读取仍在后台继续。准备及根/bridge 登记回调在继续下一阶段前检查到期与停止状态。超时收尾记录为 `TimeoutError`；已经登记的会话必须停止实际进程并持久终结，没有启动进程的会话保留明确的 not-started 证明。已开始的 I/O、数据库提交和进程停止仍须实际完成，因此 45 秒是停止继续推进的期限，不是保证响应在第 45 秒瞬时返回。

未修改 CLI 进程返回的初始化结果，没有替代 CLI 二进制、RPC 响应或模型推理。测试使用 PATH 上实际安装的原生 `codex.exe`，隔离数据库、算例存储和私有 home，不读取个人 Codex 登录状态。

## 实际启动验收

显式入口：`platform/server/live_llm_cli_startup_acceptance.py --source-profile-id 26 [--postgres]`。`--durable-only` 用于单独验证幂等入口；它仍调用真实模型读回证据。

执行前审查了新入口、`cli_startup_deadline_checks.py`、真实上游记录器 `live_llm_acceptance.py`、`testsupport.py` 和实际迁移函数。旧 `codex_actual_cli_smoke.py` 等包含固定 Responses 输出的脚本只作源码审查，没有执行，也没有放宽回归登记。

| 场景 | 验收内容 |
| --- | --- |
| CLI 发现、私有目录准备、程序摘要后到期 | 包裹实际同步操作暂停，到期仍等待线程返回；不创建会话记录、不启动进程 |
| owner 提交后到期 | 实际提交后暂停，保留已知 owner；释放暂停后登记 failed/TimeoutError 和未启动证明 |
| 真实 CLI 初始化后到期 | 原生程序返回实际 userAgent；超时停止进程并登记失败 |
| 激活提交后到期 | 实际 active 提交落库后暂停；到期后停止进程，最终 failed/TimeoutError，不向调用者返回活动会话 |
| 更短的父期限 | 父预算 1.5 秒，子会话继承它，不能刷新为测试的 5 秒 |
| 普通启动与关闭 | 实际程序初始化、活动记录和正常关闭成立，出生身份已退出 |
| 幂等入口期限 | 4 秒总预算中，初始实际鉴权后等待 1 秒；Session.prepare 剩余小于 3.2 秒，发现线程到期后不继续启动；同请求 ID 重试返回原失败记录 |
| 幂等入口普通启动与关闭 | 唯一活动会话、同 ID 重放不重复启动、正常关闭与真实退出成立 |

SQLite 先完成[八场景矩阵](../../tmp/live-cli-startup-y8qxrqei/cli-startup-checks.json)，之后单独完成[两个幂等场景](../../tmp/live-cli-startup-yimjl929/cli-startup-checks.json)；PostgreSQL 完成[全部十场景](../../tmp/live-cli-startup-1yi_pfdr/cli-startup-checks.json)。生产实现一致，后续只扩展验收入口。合计 **20 项实际基础设施检查**，8 个登记根进程分别核验出生身份已退出。三套 API 已退出，[临时 PostgreSQL 数据库已删除](../../tmp/cli-startup-postgres-metglrpi/result.json)。

## 真实模型证据与边界

每套实际启动结果生成摘要，写入隔离算例文件，再由真实 DeepSeek 自主调用 `read_text_file` 读取。检验完整流式字符串、工具结果和落库回答一致。三组均通过：

- [SQLite 八场景后读回](../../tmp/live-cli-startup-y8qxrqei/live-result.json)：2 次真实推理，8,632 tokens。
- [PostgreSQL 十场景后读回](../../tmp/live-cli-startup-1yi_pfdr/live-result.json)：2 次真实推理，8,564 tokens。
- [SQLite 幂等入口后读回](../../tmp/live-cli-startup-yimjl929/live-result.json)：2 次真实推理，8,772 tokens。

服务与模型均为 `api.deepseek.com / deepseek-flash`，共 **6 次真实调用、全部 HTTP 200、25,968 tokens、模拟 LLM 0**。三项模型端到端读回与 20 项基础设施检查分列，没有使用固定模型回复或模拟 CLI 推理。

这些模型回合发生在平台 Agent API；本次 CLI 只启动并初始化，没有发起 CLI 内部模型回合。报告的 `cli_model_inference_verified=false` 明确保留这一边界，不能据此宣称 CLI 推理、工具审批或续轮已经真实模型验收。

## 继续交付

本次期限从会话服务启动起算，HTTP 请求体/认证、幂等请求准入至后台执行之前的时间尚未纳入。Windows 实际路径已经验证；共享代码的 WSL 改动仍需真实 WSL 专项验收，不能用 Windows 结果替代。

S3 staging 下载仍在 case 写锁内，下载循环尚未在每次读取前后检查共享期限；私有 CLI 状态、临时文件、会话运行期间的磁盘/输出/全局并发预算和回收仍待。下一步先处理 S3 准备的期限与锁范围，再继续私有目录额度、WSL、turn/fork 的完整生命周期及真实 CLI 模型联调。

主线完成 33/53、剩 20；资源完成 11/17、剩 6。295 项全功能目标继续，整项终验未完成，本轮不减少剩余大项数量。
