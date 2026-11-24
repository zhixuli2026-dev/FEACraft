# Redis worker 停机与收尾

2026-11-16，N4-07 的同主机 worker 关闭流程修复。主线仍为 48/53；这份证据不关闭整项 N4-07。

## 原问题与实现

arq 取消 delivery 协程时，`JobProcess.wait()` 保护的实际作业仍可能继续运行；旧代码先移除 ACTIVE 登记，随后关闭数据库连接和事件循环。这会让停机早于原生进程退出和作业最终记录。

现在关闭 worker 先禁止领取任务、结束领取循环，默认给已领取任务 30 秒完成时间。`close(grace_seconds=0)` 立即请求中断；其他非负值覆盖等待时长。达到等待期限后沿用同一中断路径。所有 delivery 完成收尾后才关闭 Redis 和数据库连接；单个 delivery 出错也先等待其他 delivery 收尾。

Redis delivery 的启动交接受保护；关闭请求等启动交接完成后停止实际执行，再等待原有进程停止、日志及 SQL 收尾流程。执行中的任务记录 `interrupted/recovery_required`，停止凭据沿用 `ownership_lost`。已经开始的 finalizing 继续完成；已接受的用户取消仍为 `cancelled`。arq 对中断 delivery 的重投沿用 SQL 已认领/终态检查，不自动重跑已认领任务。

## 实际验收

入口：`platform/server/live_llm_worker_shutdown_acceptance.py`。使用隔离 SQLite/文件存储、实际 WSL Redis、Windows arq worker 和非特权 WSL 命令；没有模拟模型或推理。

- 正常关闭：命令在宽限期内完成，作业成功后 worker 退出。
- 紧急关闭：运行中的长命令中断，原生停止凭据验证通过，SQL 记录 interrupted。
- 宽限期耗尽：同样完成中断和收尾。
- 用户取消：通过实际 API 发起，保持 cancelled，验证原生停止凭据。
- 重启：新的 worker 执行 convert 命令并发布随机证明文件；真实 DeepSeek 选择 read_text_file 并读回随机字符串，验证流式回复和持久历史。

首轮证据：`tmp/worker-shutdown-gfdljkwd/`，4 个实际作业、6 个 worker、2 次 DeepSeek HTTP 200。最终版本证据：`tmp/worker-shutdown-uhwuof9b/`；详细结果以该目录的 `shutdown-result.json`、`live-result.json`、`job-exits.json` 为准。审查登记为 `tmp/N4-07-worker-shutdown-review-v2-20261116.json`，继承辅助代码审查并核对全部 595 个源码散列。

## 尚未覆盖的原计划范围

本轮是同一 Windows 主机上的 Redis worker 和实际 WSL 执行环境，存储为 FS/SQLite；不代表跨主机、S3/PostgreSQL 或 Linux 宿主 worker 的完整验收。启动窗口停机、finalizing 窗口停机、SQL 最终排空失败和多任务故障组合尚未由本轮入口单独覆盖。

N4-07 原有剩余要求继续有效：外部主机归属、运行环境映射、远端停机/看门狗与接管，以及最终排空记录失败的恢复。未验证远端执行已停止时仍保持需处理状态。本次复用既有停止凭据和恢复机制，没有增加新隔离层或扩大阶段退出条件。
