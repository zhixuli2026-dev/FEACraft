# 恢复快照请求的共享期限与取消收尾

2026-11-07，接续[镜像清理入站期限](image-ingress-deadline.md)。恢复快照原先只有原生执行的局部超时，前置退出核验、节点空间准入、配置写入和创建执行记录不共享预算。创建记录已经提交但调用者尚未拿到 owner 时取消，也缺少明确的终结处理。

## 实现

恢复 POST 路由从认证依赖开始设置默认 300 秒期限，服务调用继承更早期限；直接调用恢复服务也有同样预算。获取恢复并发槽、前置退出核验、准入、配置、owner 创建、原生放行前复核、原生执行和结果读取均检查同一个截止时间。到期后不继续新的阶段，原生执行器继续使用其既有的更早期限与实际停止/退出核验。

节点额度、配置写入和 owner 提交保留实际回执并等待受保护操作结束。owner 已提交而原生执行尚未开始时，将记录明确终结为 failed / TimeoutError；其他取消保留取消类型。额度释放要求已创建的 owner 不再 active，且所有已登记子进程均有已核验的停止记录。没有完整证据时继续保留额度。

结果文件读取和查询中的文件校验也等待实际线程结束。最终执行记录提交由原有受保护的收尾完成；若提交已经成功而请求期限到达，HTTP 返回 `502 / sandbox_recovery_incomplete`，查询仍显示实际 succeeded 记录。不会将已提交成功改写为失败。恢复创建接口仍是创建独立操作，重复 POST 不声明为同一个幂等请求。

路由期限覆盖处理器，不包含代理/HTTP 头接收、响应传输和不可中断系统 I/O 的硬性终止。原始镜像和未完成恢复副本的后续维护不由这个期限代替。

## 验收与已发现的问题

显式入口为 `live_llm_recovery_deadline_acceptance.py --source-profile-id 26 [--postgres]`。基于真实 WSL 写入、实际 API 退出和进程退出观察，在正常创建恢复快照之前测试七个位置：前置核验、节点准入、配置写入、owner 提交、原生放行前复核、结果读取、完成记录提交。暂停均发生在实际操作或实际提交之后，不替换操作结果。准备阶段采用 12 秒预算，原生阶段采用 45 秒预算。

模型阶段继续用用户授权的真实 DeepSeek，让模型选择工具读取真实恢复文件中的随机证明，验证流式回复与落库。运行前审查了真实模型适配、退休准备、隔离服务器、临时 PostgreSQL、迁移及实际出生身份核验辅助代码；没有运行历史模拟集合或修改审查登记绕过限制。

首轮 SQLite 在节点准入后的额度断言失败：[记录](../../tmp/sandbox-recovery-ui-retirement-867whcte/recovery-deadline-checks.json)。原断言要求旧额度集合完全不变，但生产准入会依据实际 owner 退出证据回收旧 API 的遗留额度。已改为分别检查本次额度没有遗留、仍保留的旧额度未改写、消失的旧额度确有实际所属进程退出证明。首轮模型调用 0、模拟 0，5 个已登记进程核验退出；失败记录保留。

第二轮在 owner 暂停处因验收脚本读取合法空记录 `{}` 的 `children` 键而失败：[记录](../../tmp/sandbox-recovery-ui-retirement-inkbou3k/recovery-deadline-checks.json)。前置核验、准入和配置三项已通过。脚本改为按现有记录格式读取可缺省的空子进程集合；失败收尾释放暂停并等待请求，7 个登记进程核验退出，模型调用仍为 0，未将其计为完整矩阵通过。

中间双库矩阵还暴露了原生放行前异步复核的期限缺口：停止标志能阻止之后的放行，却不能中断正在等待的回调，原回调仍等到自己的 120 秒测试期限。旧算例只断言最终响应类型，不能证明这段等待遵守请求期限。保留[修复前源代码及运行归属](../../tmp/recovery-deadline-before-release-fix.json)，不将中间矩阵的整体通过标记当作此项期限的完成证据。

现已在原生执行器中用同一 `entry.deadline` 包裹 `before_release`，可取消的异步核验到期退出；回调内部受保护的文件/提交操作仍须实际结束。验收新增放行前到期后 10 秒内完成实际停止和响应的断言，并完整重跑双库矩阵。

## 最终代码的实际结果

SQLite：[七位置矩阵](../../tmp/sandbox-recovery-ui-retirement-jfj5az20/recovery-deadline-checks.json)、[真实模型](../../tmp/sandbox-recovery-ui-retirement-jfj5az20/live-result.json)。七项均通过，放行前复核在共享期限后约 0.719 秒内完成实际进程停止核验及响应。owner 提交后超时变为 failed / TimeoutError 且没有子进程；完成提交后超时保留 succeeded 查询结果。真实 DeepSeek 2 次、8,408 tokens，严格只回复证明字符串通过。

PostgreSQL：[七位置矩阵](../../tmp/sandbox-recovery-ui-retirement-pk7nd1mq/recovery-deadline-checks.json)、[真实模型](../../tmp/sandbox-recovery-ui-retirement-pk7nd1mq/live-result.json)、[临时库删除](../../tmp/image-postgres-opatwzps/result.json)。七项均通过，放行前超时约 0.672 秒内完成实际停止核验及响应。真实 DeepSeek 2 次、8,364 tokens，真实工具选择、正确文件内容、流式回复和落库检查通过；**严格只回复证明字符串未通过**，模型在证明字符串前输出了“我来读取该文件。”。没有删除开场白或将此项改写为严格格式通过。

镜像清理兼容：[受保护归档](../../tmp/sandbox-recovery-ui-retirement-1ehu5irq/http-request-deadline-checks.json)、[模型](../../tmp/sandbox-recovery-ui-retirement-1ehu5irq/live-result.json)。新的 `before_release` 期限不会丢弃受保护归档线程，超时停止和后续清理通过，真实模型 2 次、8,478 tokens，严格读回通过。

Agent Shell 兼容：[真实模型与实际 PowerShell](../../tmp/live-shell-deadline-551es0_0/live-result.json)。真实 DeepSeek 自主选择工具执行 25 秒等待命令，工具总预算 12 秒；实际准备暂停 5.016 秒，放行时剩余约 6.797 秒，超时后实际核验停止，模型报告工具失败。真实推理 2 次、9,486 tokens。该算例要求如实报告工具结果，不要求纯证明字符串格式。

最终代码合计 18 项期限/资源基础设施检查、4 项真实模型内容/工具验收，严格字符串格式另有 1 项未通过。退休准备继承的权限、损坏记录等协议/故障检查单独保留，未将它们计为模型验收或本批新增覆盖。最终三套沙箱共 37 个登记出生核验退出，另有实际 Shell 宿主退出证明。

中间两套真实模型读回也保留：[SQLite](../../tmp/sandbox-recovery-ui-retirement-_79fc7cq/live-result.json)、[PostgreSQL](../../tmp/sandbox-recovery-ui-retirement-h8drcui6/live-result.json)。各 2 次真实推理，分别 8,356 / 8,354 tokens，严格读回均通过；它们不证明后来修复的放行前异步等待期限。中间 PG 临时库[已删除](../../tmp/image-postgres-kmq0kh7a/result.json)。

本轮连同中间验证，真实 `api.deepseek.com / deepseek-flash` 推理共 12 次，HTTP 全部 200，共 51,446 tokens，模拟 0。两次早期验收脚本失败均没有模型调用。所有七套沙箱的 75 个登记出生及另一个 Shell 宿主均已核验退出，相关 API 已停止，两套临时 PostgreSQL 数据库均已删除。

## 继续范围

继续定位严格回复中的真实模型开场文本，区分工具前讲解和最终答案，不能靠删除输出制造通过。后续推进 staging 清理、其他 native request/CLI/文件入口的整段期限和 I/O/队列限制、全局并发额度。代理/HTTP 头与响应传输、不可中断系统 I/O、迟到交接/API 退出交错、原生后续 330 秒期限、偶发退出核验拒绝/时延，以及真实重启/空间/权限故障仍待。

主线仍完成 33/53、剩 20，资源完成 11/17、剩 6，全部 295 项最终交付目标保持。本项覆盖没有计为整个 N4-05 完成，严格格式未通过也没有因真实服务返回 200 而忽略。
