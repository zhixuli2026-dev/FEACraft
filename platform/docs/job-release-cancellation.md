# 已提交取消与作业启动门的同序处理

2026-11-03，N4-04 启动竞争修复。N4-03 的专业入口核对发现了通用执行器的一个窗口：登记持久化成功之后，原生启动门放行之前，另一个 API 已提交取消，启动端仍然调用 `OwnedProcess.release()`。后台心跳之后能观察取消，但已经不能阻止命令最初的副作用。

## 实际复现

两个隔离 API 共享一次性数据库和存储，在提交作业前都已启动。第一个 API 的测试检查点位于真实进程身份登记提交之后；第二个 API 的内存注册表中没有该进程，通过正式 `POST /jobs/{id}:cancel` 提交取消。只读查询确认 Job 为 cancelled、attempt 为 cancelling，且载荷标记尚不存在。

解除第一个 API 的检查点后，真实 Python 载荷仍写出了标记。载荷本身只有标记写入和 2 秒等待，最后实际退出、持久结果仍为 cancelled。该证据证明“取消提交之后仍放行”，不将其描述为最终状态被成功覆盖或无限残留。证据位于 `tmp/cancel-release-before-2o5jia5l/before-result.json`。

## 修复后的顺序

`job_execution.release_process` 在实际登记完成之后复核持有的进程身份，再取得与取消写入相同的 case 数据库锁。锁内重新检查：

- 当前 claim、active attempt、worker 和 running 阶段仍一致；
- 尚无已提交取消请求，Job 仍为 running；
- 租约有效，数据库登记与本次实际进程的完整绑定相同。

检查通过后，在持有该锁期间发送启动门放行消息，控制管道发送最多等待 5 秒，然后结束事务。耗时的宿主/WSL 身份探测在锁外进行，不把它纳入 case 写事务。

因此，取消先取得锁并提交时，后续放行被拒绝；放行先取得锁时，取消必须等待放行事务结束，再按运行中取消处理。这里的先后指两个数据库控制边界的顺序，不把收到 HTTP 请求的网络时间当作已经提交。

如果控制消息已经发送，而随后事务提交失败，载荷可能已经运行。执行器会停止本次实际范围并记录失败，不承诺“提交失败就一定没有副作用”，也不自动再启动一次。

`JobProcess.start` 在登记函数正常返回、随后未能正常放行时执行所属停止，再关闭启动资源；实际停止回执由原 `finish` 绑定到该 attempt 持久保存。取消先提交的情况保留 cancelled，不因启动流程抛出冲突而覆盖成 failed。登记调用尚未正常返回的启动失败继续遵守原来的清理契约；提交中断造成的登记结果不确定仍需 N5 对账，不能给没有确认绑定身份的记录伪造停止回执。

提交与取消 HTTP 的现有响应契约保留：取消赢得启动窗口时，原提交返回关联 Job 的 409，查询可看到 cancelled 及实际停止证据；终态重复取消返回 `ok=false` 且不改写回执。N4-04 的其余接口契约和矩阵另行推进。

## 验证范围

`job_release_smoke.py --postgres` 在 SQLite / PostgreSQL 各运行 8 项：

| 场景 | 运行时 | 关键证据 |
|---|---|---|
| 正常放行 | Windows、WSL | 实际载荷标记、成功终态、所属退出与同一持久回执 |
| 取消先提交 | Windows、WSL | 取消时载荷未运行，解除检查点后仍不运行；登记的启动门退出，保存 cancelled 停止回执 |
| 放行锁先取得 | Windows、WSL | 对端取消在检查点处不能先完成；随后取消提交，即使通知失败，心跳仍观察持久取消并终止载荷 |
| 放行前租约过期 | Windows | 不发送放行，载荷未运行，失败终态及实际退出 |
| 放行后的提交失败 | Windows | 注入本次事务提交异常，所属范围退出；失败及停止回执持久保存，不自动重放 |

每项都通过另一 API 读取相同停止回执并查询实际进程已退出，外部哨兵持续存活；另核验跨用户读取拒绝。两个 API 都在 Job 创建前启动，本测试没有规避或解决 N5 现有启动全表 interrupted 对账问题。

通知故障由测试替换 Redis 发送函数实现，没有启动真实 arq worker，也没有把模拟通知失败计为真实 Redis 断线重连验收。当前运行中备用轮询仍是原有 10 秒心跳，不将本测试声称为新的取消时延保证。

通用领取、提交幂等、登记、最终收尾、双 API/SSE 终态、普通文档及 FloTHERM 复制编译的定向回归另存规范证据 `tmp/mainline-N4-job-release-evidence/`，具体结果与原始日志见 `evidence-origin.json`。源码基线为 `platform/backups/mainline-N4-job-release-20261103.zip`，SHA 和逐文件匹配另存；没有在恢复目录重跑。

本批没有 DDL、迁移、UI 或开发服务变更。原生子执行和 NativeRequest 的后续放行检查见 [原生放行顺序](native-release-cancellation.md)；PTY/viz、CLI 的独立生命周期、其余排队重投、claim 和会话竞争、真实 Redis 多 worker、预算/配额释放及 N5 恢复仍保留在既定主线与全功能台账中。

```powershell
platform/server/.venv/Scripts/python.exe -X utf8 -B platform/server/job_release_smoke.py --postgres
```
