# 原生范围关闭与独立对账的提交中断验证

2026-11-07。新增显式真实模型入口 `live_llm_scope_commit_acceptance.py` 和实际故障矩阵 `sandbox_scope_commit_checks.py`。测试在真实 Windows/WSL 进程与隔离数据库上执行，故障位置调用 `os._exit(81)` 终止实际 API；没有替换 LLM 回复、工具选择或流式结果。

## 验证边界

先在原生预登记完成、原生执行许可尚未发出的位置取消内部准备任务，再依次覆盖以下六处退出。关闭凭证与对账写入使用实际事务，提交前退出前先 flush，避免只验证“尚未尝试写入”的情况。

| 退出位置 | 退出后实际原生范围 | 已提交 closed | 已提交独立对账 |
| --- | --- | --- | --- |
| Windows 守护回执返回后 | 空范围 | 否 | 否 |
| 原生范围撤销返回后 | 不存在 | 否 | 否 |
| closed flush 后、commit 前 | 不存在 | 否 | 否 |
| closed commit 后 | 不存在 | 是 | 否 |
| 独立对账 flush 后、commit 前 | 不存在 | 是 | 否 |
| 独立对账 commit 后 | 不存在 | 是 | 是 |

每处核对实际所有者、Windows 宿主、守护与 Linux 原生出生身份已退出；重启后 GET 不修改记录，镜像和快照仍保留。显式重试后核对六份独立对账、七份 closed 和唯一清理完成事件；原始 children/result 不被改写，不补造正常停止回执，已提交凭证保持不变。重复 POST/GET 返回同一结果，保留快照字节不变，实际校验和管理准入重新通过。

## 实际结果

- SQLite：[范围与事务检查](../../tmp/sandbox-recovery-ui-retirement-0yukcxf7/scope-commit-checks.json)记录六处退出和一项恢复汇总检查，共 7 项实际基础设施检查通过；[模型结果](../../tmp/sandbox-recovery-ui-retirement-0yukcxf7/live-result.json)记录 1 次真实 `api.deepseek.com / deepseek-flash` 推理 HTTP 500。因此整体 `passed=false`，模型读取验收失败。
- PostgreSQL：[范围与事务检查](../../tmp/sandbox-recovery-ui-retirement-d1ups2_b/scope-commit-checks.json)同样 7 项实际基础设施检查通过；[模型结果](../../tmp/sandbox-recovery-ui-retirement-d1ups2_b/live-result.json)记录首轮推理 HTTP 200，真实模型选择 `read_text_file`，实际读回恢复文件，成功响应 usage 为 4,138 tokens；后续第二轮推理 HTTP 500，整体仍失败。[临时数据库已删除](../../tmp/image-postgres-5vrusloc/result.json)，未修改开发数据库。
- 同期[服务复查](../../tmp/live-deepseek-wf3cekho/live-result.json)：模型列表 200、真实推理 500。该检查不等于模型回复通过。

### 保留的失败与限制

1. [首次 SQLite 运行](../../tmp/sandbox-recovery-ui-retirement-j6j5l4qf/scope-commit-checks.json)通过前四处后，在第五处前置断言失败。取消内部准备任务实际产生 child `CancelledError`、请求 `failed/ChildExecutionError`；原测试错误地写死请求 `cancelled`。已按实际错误传播修正断言，并仍要求结束时间、关闭凭证和后续对账。
2. [第二次运行](../../tmp/sandbox-recovery-ui-retirement-n__21bfr/scope-commit-checks.json)通过前两处后恢复返回 409；[第三次运行](../../tmp/sandbox-recovery-ui-retirement-chx050p9/scope-commit-checks.json)通过前三处后返回 409，内部原因为 `process_exit_unverified`。[事后只读观察](../../tmp/sandbox-recovery-ui-retirement-chx050p9/postfailure-birth-observations.json)显示原身份均已退出，不能反推拒绝当时的状态。具体原因尚未证实，不能据后续通过称为已修复。
3. 新增诊断只记录真实核验返回值和必要的只读身份观察，不改变核验结果、不把 mismatch/unverified 改成 exited，也不自动重试吞掉失败。
4. 真实模型 500 保留为失败，无模拟回退。失败响应没有成功 usage，不推算其 token 消耗；仅记录 PostgreSQL 首轮实际返回的 4,138 tokens。准备阶段失败的前三套没有执行模型调用。
5. [额外只读诊断](../../tmp/scope-exit-observation-diagnostic-20261107.json)在已保存的六份真实出生身份之间轮换，共 30 次实际退出核验均通过，耗时 35.27 秒；未复现先前偶发拒绝。这不是修复证据。没有数据库修改或进程终止，LLM 调用 0。

本轮四套 SQLite 尝试与一套 PostgreSQL 共记录 21 次定点 API 退出（4+2+3+6+6）；最终两套合计 14 项范围/提交/恢复检查通过。基础退休算例中的权限和损坏注入仍单列为协议故障，不能混入模型或进程证明。本轮真实推理请求共 4 次（服务复查 1、SQLite 1、PostgreSQL 2），HTTP 成功 1 次、失败 3 次，完整模型验收成功 0 套，模拟 0。此前用户提醒轮的 2 次失败单独保留，不重复计入。

## 下一步与全量范围

继续定位偶发退出核验拒绝；补齐原生 unlink/complete.json 中断、准备/许可/登记位置双 API 竞争、迟到创建/加入、错误许可、原生与守护期限、目录权限/实现升级/重启/空间故障及整个请求的资源期限。快照完整恢复、其他副本维护和剩余资源入口继续按[资源清单](resource-budget-worklist.md)和[主线计划](next-stage-mainline-plan.md)推进。

主线 33/53 完成，剩余 20 项（3 项进行中、17 项待办）；资源 11/17 完成，剩余 6 块。295 项全功能最终验收要求仍有效。本矩阵只增加上述边界的证据，不增加整项完成数，也不声称覆盖宿主断电或 WSL 重启。
