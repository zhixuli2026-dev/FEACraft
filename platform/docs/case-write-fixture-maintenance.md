# 并发写入回归夹具维护与终端活动缺口

N5-01不可变发布校验已移出事务，以完整本地绑定证明约束最终提交；心跳/取消在输入与包哈希期间可提交。另修复跨API终端活动复制缺口，持久会话/暂存检查与新登记使用同一算例控制锁。发布专项双库各21、终端活动各17、完整并发写入各93通过；工程5、文档5、生命周期5套兼容经原始失败保留与单套复核完成，终端执行44/暂存9项通过。可编辑来源/递归依赖/封存/别名长事务及恢复仍待完成；head4e2da9f3c865/35表、53/295与11完成/18实施中/24待实施不变。 规范证据见 `tmp/mainline-N5-publication-verify-evidence/`。

2026-11-03，在[发布前不可变文件校验](publication-verify-lease.md)兼容回归中发现。终端活动保护已补入算例管理与会话登记，专项双库各 17 项、完整并发写入各 93 项通过；终端执行 44 项及暂存 9 项兼容通过。本页保留夹具失败、产品缺口复现及修复证据。发布单元的完整 LaTeX 复核亦已通过，原分组失败记录保留。

## 旧夹具与当前流程

`case_write_smoke` 初始在导入时失败，尚未执行业务断言。它依赖已删除的 `reports.prepare_report_snapshot`、`reports._prepare_files`，会话部分还依赖 `viz._launch_locked` 和旧 `PtySession`。最初涉及的路由与夹具均与上一冻结基线一致，核对见 `tmp/publication-verify-case-write-diagnosis.json`；不能归因为本轮发布校验改动，也不能将该套件计为通过。

当前夹具改为以下真实入口：

| 原检查 | 当前检查与保持的性质 |
| --- | --- |
| 报告准备期间阻塞编辑 | 暂停注册处理器 `complete_preparation`，允许编辑／读取，提交前因源内容变化拒绝 Job 与发布 |
| 旧报告私有快照目录 | 实际 `attempt_workspace.Snapshot.prepare` 的唯一目录；已有目录必须保留，不提交新 Job |
| 取消旧快照复制 | 实际 `seal`／准备线程；重复取消先排空线程并清理本次目录，再释放编辑者 |
| 按 `report_worker.py` 字符串拦截启动 | 注册处理器提交后、实际 `start_job` 前暂停；取消和回收站操作不得被迟到启动撤销 |
| claimed 作业不能取消 | 当前取消命令持久化终态；暂停后的启动继续时必须拒绝，不能运行命令 |
| 任意错误都期待 409 | 非法远程图片在 Job 创建前返回 400；已变化的输入与状态冲突仍返回 409 |
| 旧 PTY 构造函数 | 实际 `TerminalSession`／`OwnedTerminal.start_bridge`，记录根和 PTY gate 出生身份 |
| 旧可视化锁内启动 | 实际 `VizSession.start`，登记后读取可用，活动会话阻止删除，取消仍核验进程退出 |
| 禁用账户连接后关闭 | 当前在 WebSocket 握手前返回 403；有效账户访问错误算例仍检查连接后的 4403 |

没有恢复已退休的生产执行路径来适配测试，也没有删除目录不得覆盖、旧授权重验、原生退出、取消排空等核心断言。

## 本轮证据

- 完整套件第一次修复后已进入业务断言，随后在远程图片的旧 409 断言处失败：`tmp/publication-verify-case-write-repaired/`。定向准入第一次同样失败：`tmp/admission-fixture-repair-first.log`。
- 修正为当前非法输入 400 契约后，准入定向 12 项通过：`tmp/admission-fixture-repair-oznextlp/result.json`。
- 会话定向第一次在禁用账户握手前的 403 处暴露旧断言：`tmp/session-fixture-repair-first.log`。修正后 12 项通过：`tmp/admission-fixture-repair-wyyg5oqh/result.json`。下述额外诊断表明，其中一个“因终端活动拒绝删除”的判断仍过弱，不能用这份通过结果证明跨实例活动保护。
- 原夹具修复版本分别保留在 `tmp/fixture-repair-v1/` 和 `tmp/fixture-repair-v2/`。定向检查不替代完整 `case_write_smoke`。

## 已复现的产品缺口

`case_admin._copy_source_idle` 与 `assert_no_case_activity` 查询本进程的终端列表，没有像 CLI／可视化路径那样查询持久终端记录。因此另一 API 实例可能看不到活动终端。

真实双 API／PTY 复现：本实例返回活动终端冲突，另一实例却成功复制同一算例；复制前后根进程与 PTY gate 的实际出生身份均匹配。随后显式关闭终端，核验持久清理成功，并回收测试副本。证据：`tmp/terminal-copy-before-t1g0sqgk/before-result.json`，脚本 `tmp/check-terminal-cross-api-copy-before.py`，原产品代码哈希一并保存。

此前删除试验没有成功删除算例：Windows 目录占用返回 409，见 `tmp/terminal-delete-before.log`。这是一条必须保留的反向结果，不能把它描述为已发生删除，也不能把文件系统的偶然拒绝当作应用识别了持久会话。

夹具现在要求本实例和另一实例对算例复制、工作区复制、算例删除、工作区删除都明确返回终端活动冲突。启动取消窗口也检查同一原因。产品专项与加强后的完整并发写入套件均已在双库通过这些检查。

## 实现与专项验收

上述两个活动边界现已查询 `NativeSession`，通过 `terminal_records.public` 核对阶段和清理证据。活动、租约过期、损坏证据及尚未核验停止的记录均不能授权文件系统变更。失败且缺失根记录也不能直接推断为从未启动，应等待对账；已经核验停止的关闭／失败记录还须确认 staging 目录、清单及清理标记均不再残留。非法暂存前缀同样拒绝。没有数据库迁移。

登记侧也已使用同一算例控制锁：原先 `terminal_records.create` 在选择算例的锁释放后独立插入记录，仅增加活动查询仍会留下检查与登记之间的竞争。绑定算例的终端现在于登记事务内取锁并复核算例可用性，再开始初始租约计时，避免等锁消耗租约；未绑定算例的终端保持支持。选择后、登记前被删除的算例在创建记录和原生启动前被拒绝。

新增 `terminal_case_activity_smoke.py --postgres` 在双库上各通过 17 项，覆盖启动登记、真实活动终端、退出但尚未提交停止回执、过期租约、损坏清单、缺失停止回执、未登记根的失败状态、三种暂存残留、非法暂存前缀、已清理暂存、不相关算例、关闭后的复制／回收站／恢复，以及选择后删除与登记的竞争。结果见 `tmp/terminal-case-activity-evidence-q4wm6sev/result.json`；临时 PostgreSQL 已删除。暂存检查使用实际本地文件，不代表真实 S3 验收。

新专项前两次失败亦保留：`tmp/terminal-case-activity-dual.log` 中无关算例被单独回收，导致工作区提前返回不可用；夹具改为恢复无关算例，并把测试副本放到独立目标工作区。`tmp/terminal-case-activity-dual-second.log` 中过期状态注入保留了结束时间，违反既有数据库约束；夹具同时清空并在恢复时还原结束时间。产品代码在这三次运行间未修改，第三次结果为 `tmp/terminal-case-activity-dual-third.log`。

完整并发写入首次加强检查也被前序场景留下的已回收算例提前拦截，见 `tmp/publication-verify-case-write-final.log`。会话夹具给真实终端断言使用独立工作区，未改变产品；完整复核 `tmp/case-write-evidence-2e8m5bj8/result.json` 为 SQLite／PostgreSQL 各 93 项通过，临时库已删除。终端执行／暂存兼容 `tmp/publication-verify-terminal-compat/summary.json` 为 2/2，通过 44／9 项；此两套使用 SQLite，实际 Windows／WSL／sandbox、无绑定算例终端均覆盖，S3 仍为本地受控适配器。
