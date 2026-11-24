# 发布前原生验证与提交边界

后续正式整合：报告/LaTeX提交及工程报告原生准备已移出case写事务；HTTP断线、幂等和来源重验通过本批隔离验证，详见 [document-admission.md](document-admission.md)。原有正式作业、独立内容验证与发布协议继续适用。

后续增量：独立准备请求归属基础及双库各14项验证已落地，正式业务锁外准备、来源重验和HTTP断开仍在下一单元接通；见 [native-request-scopes.md](native-request-scopes.md)。本页正式作业协议继续适用。

后续新增成功发布后的兼容收尾可见性标记；本页竞争专项双库各10项在该协议上重跑通过。发布一旦提交仍不可被迟到取消撤销，收尾与恢复状态详见 [job-terminal-projection.md](job-terminal-projection.md)。

正式报告的原生解析/复编译现已接入API所属Pandoc子执行；验证返回后先封闭并等待finalizer残留调用，再进入终态事务。此协议仍不持有case原生验证锁，发布竞争专项在Windows短命成员观察竞态修复后重跑双库各10项通过，见 [owned-pandoc-children.md](owned-pandoc-children.md)。

后续LaTeX独立复编译已接入API所属原生子执行，并随父级取消停止、核验退出和持久回执，双库各15项；本页发布竞争另重跑双库各10项通过。报告Pandoc及准备探测仍待入口监督，见 [owned-native-children.md](owned-native-children.md)。

本批最终证据：双API/SQLite和临时PostgreSQL各10项通过；进程树双库各15项、core14/14、execution3/3、收尾与文档/几何4/4、网格/后处理/求解3/3通过。详细来源与顺序见tmp/mainline-N4-preflight-evidence/evidence-origin.json；没有将旧批结果算作本轮重跑。

日期：2026-11-02。对应 N4-03 的编译子执行接线前置工作，以及 N4-04 的持久取消/发布竞争增量。两项均未整项完成；完整范围见 [主线计划](next-stage-mainline-plan.md)、[全功能交付计划](full-function-delivery-plan.md) 和 [原生入口任务](native-entry-supervision.md)。

## 问题与实现

此前 `job_execution.finish → artifact_publication.finalize → handler.artifacts` 在持有 case 写事务时独立验证产物。LaTeX 会重新启动 WSL 编译，报告会重新运行 Pandoc。长时间占有事务不仅阻塞同 case 编辑、取消和心跳，也使将来的子执行登记无法安全使用独立 SQLite 写事务。

现在分两段执行：

1. `prepare_for_finish` 用短事务复核当前 claim、活动 attempt、有效处理器绑定和已有进程清理记录，取得输入、输出、发布预约的完整快照，然后关闭会话、释放写锁。非成功状态、已终止任务、无发布预约或未核验的清理结果不会进入原生验证。
2. 在事务外验证 attempt 文件、执行 `handler.artifacts` 的独立复算/复编译，保留结果及失败信息。此时取消、心跳、其他 case 写入或较新发布预约可以提交。
3. 最终事务重新验证所有权和当前取消状态。只有同 case/job/attempt/claim、相同存储前缀及输入/输出/预约快照的验证结果可使用；另一次尝试的结果不能套用。
4. 再次检查封存输入，复制产物时逐文件复核哈希。仍在锁内检查发布代次、工程来源、目标前置条件并提交活动指针。输出在验证后被改写会失败；较新代次已预约时旧代次只能成为 superseded；取消、来源变化或验证失败保留之前的有效发布。

`PreparedValidation` 仅是本次调用的内部内存对象，不接受客户端提供，也不是可在重启后直接复用的持久恢复凭据。描述符是从原预约解析出的独立副本，验证期间的派生产物清单修改不改变原预约快照。终态、publication、process_stop 与 outbox 的提交关系保持原契约。

报告的锁内来源复核也曾重新调用 Pandoc。现在它使用注册描述符中已经解析且经过独立复编译验证的资源集合，重新读取所有选中文件的大小/哈希，包含 Markdown 本身，并继续核对 case 语义和后处理门禁。后续锁外准备还保存并重验资源候选优先级，因此新增同名优先资源即使没有改变Markdown或原图，也会被检测；资源内容变化、路径异常和链接继续受原文件检查约束。输入准备和独立验证仍使用真实 Pandoc，不用字符串解析替代文档验证。

## 取消接口修复

原接口依赖本机 `JobProcess.status == running`：另一 API 没有本机对象时会拒绝取消；根进程已成功退出、但仍在复编译/发布验证时，本机也会拒绝。这与持久 Job 仍在 running 的事实不一致。

现在 `POST /jobs/{id}:cancel` 先按持久状态判断并提交取消，再通知本机执行器或 Redis。取消提交失败不会先向本机进程发出信号；已有成功结果不会被迟到取消改写。没有本机对象的另一 API 同样可以记录请求，现有 owner 心跳会观察到该状态。

返回 `ok=true/status=cancelled` 仍表示请求已接受，不表示原生编译器已经退出。attempt 的 cancelling/finalizing/终态及清理记录继续区分请求与实际完成。尚未接通的内部 WSL/最终复编译子进程仍需 N4-03 的父级控制、持久身份、停止和退出等待；本批不能据此关闭 V09/V11 或完整 N4-04。

## 隔离验证

`publication_preflight_smoke.py` 使用两个 API 共享同一测试库及目录，以独立原生验证内部的明确屏障控制竞争窗口。报告通过真实 Pandoc 生成与复编译，LaTeX 通过真实 pdflatex 生成与复编译。测试 app 拦截最终提交上下文中的 `subprocess.run`，验证这些 Pandoc/WSL 调用没有进入提交事务；生产应用没有测试开关。

| 场景 | 要求 |
|---|---|
| 另一 API 在报告验证中取消 | 请求可提交；旧任务最后 cancelled，不替换原版本 |
| 本机 API 在报告验证中取消 | 不被内存中的原生 exit 0 误判为已经完成 |
| 取消提交失败 | 返回失败，没有提前调用本机 cancel；持久任务仍可正常完成发布 |
| 验证期间修改 Markdown | 编辑能提交；旧任务以 input_stale 失败 |
| 验证期间修改图片 | 资源哈希变化阻止旧结果发布 |
| 验证期间提交更新代次 | 新版本可先完成；旧任务随后 superseded，不能覆盖新 head |
| 验证之后改写 attempt 输出 | 提交时哈希复核失败，原版本不变 |
| 替换验证结果的 attempt 身份 | 拒绝跨尝试使用准备结果 |
| 独立编译验证失败 | 保留错误类型和信息，以 publication_failed 结束 |
| 另一 API 在 LaTeX 验证中取消 | 请求可提交；原 PDF 及活动版本保留 |

每个场景也复验已发布后的迟到取消不能撤销成功。屏障期间的另一 API 写入要求在 8 秒内完成，用于区分被写事务阻塞的情况，不是生产性能承诺。失败路径在 finally 释放测试屏障，测试不通过杀掉别的进程解锁。双库结果、实际耗时、兼容回归和首次发现的旧取消故障均保存在 `tmp/mainline-N4-preflight-evidence/`，以其中最终通过记录为准。

最终十项专项中，这些并发 API 写入的最长实测时间为 SQLite 1.422 秒、PostgreSQL 1.203 秒。数值只描述本机本轮测试，原始逐场景记录位于 `publication-preflight-result.json` 的 observations。

```powershell
platform/server/.venv/Scripts/python.exe -X utf8 -B platform/server/publication_preflight_smoke.py --postgres
```

全部验证使用独立目录、端口和 SQLite/严格命名的临时 PostgreSQL 库。开发库不迁移、不重载；本批无 schema 变更。

## 下一实现单元

继续 N4-03.b/c：API 持有子执行的真实 OwnedProcess 范围；Windows worker 通过仅属于本次尝试的调用通道请求内部 WSL 编译；父 attempt 的身份登记和子执行停止结果持久保存；载荷放行必须晚于登记提交，父终态必须晚于子执行核验。该子执行通道和持久记录随后已由LaTeX/Pandoc两批实现，准备阶段亦由本批锁外请求归属接通；其余入口继续执行相同要求。

输入准备阶段的工具探测、SyncTeX、PTY/viz 和 Agent/CLI 仍须按各自请求/会话语义接线。N4-04 还需排队重投、启动门前后、运行与 finalizing、发布后及取消通知失败的完整竞争矩阵；预算释放、跨 worker 通知和重启恢复继续关联 N4-05/N4-06/N5。
