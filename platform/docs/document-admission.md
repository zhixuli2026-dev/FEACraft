# N4-03：报告与 LaTeX 的锁外准备

后续SyncTeX只读请求也已接入锁外NativeRequest，历史映射/旧版来源重验、HTTP断线与实际退出、viewer权限复核通过双库各25项；继续其余探测和会话入口，详见 [owned-synctex-requests.md](owned-synctex-requests.md)。

最终验证：SQLite/临时PostgreSQL各报告12+LaTeX4项、工程报告24项（含准备中取消）、普通文档23项、Pandoc19项、原生LaTeX15项，core14/14通过。757文件基线、修复前失败和最终来源顺序见tmp/mainline-N4-admission-evidence。

日期：2026-11-02。此前的[独立请求归属服务](native-request-scopes.md)现已接入正式 HTML／DOCX 和 LaTeX 提交，以及注册工程工作流的报告准备。完整范围仍为 53 项主线、295 项功能，N4-03 保持实施中。

## 正式提交顺序

1. 授权后取得 case 控制锁，先查询幂等提交。已有请求直接返回原 Job，不因当前源文件删除、工具配置变化或昂贵解析而阻止重放。新请求只选择输入：case revision、字典原字节哈希、工程发布引用、资源范围、固定工具与源码摘要；Markdown 字节在这一阶段捕获。
2. 提交短事务，释放 case 写锁和数据库连接。通过 NativeRequest 请求归属执行 Pandoc 解析或 LaTeX 工具链探测：真实出生身份登记提交后才放行，线程携带所属通道，取消／超时等待实际原生清理。本阶段不分配 Job 或发布代次。
3. 原生准备完成后重新读取用户和算例，回到短 case 写事务；锁内重新检查访问权限和用户状态，再查一次幂等键。另一相同请求若已提交，返回该 Job；不会因这一轮准备后来源变化而重复创建作业。
4. 重新运行无原生工具的输入选择，比较准备前保存的 revision、字典原字节、工具／源码摘要、工程引用及选择范围，并复核全部已解析资源的哈希和查找路径。失效返回 input_stale 等冲突，不提交 Job。
5. 沿用原子提交和输入封存。绑定处理器之前，直接比较正式封存清单与已准备来源及 caseDict 的大小／SHA-256；中间若发生应用外文件变化，也不能提交不相符的准备结果。随后提交 Job／attempt／发布预留，并沿用原执行器放行和独立复编译。

`PreparedStep.admission` 是仅存在于本次 Python 调用中的准备数据，包含捕获的 Markdown 字节和选择条件，不写入客户端规格或发布 JSON。未完成准备的对象不能生成正式 handler record。工程产物的不可变清单、发布指针和最终内容校验继续使用原协议。

请求记录成功表示本次原生准备调用及清理完成，随后仍可能因为来源变化或权限变化拒绝作业提交；它不等于 Job 成功。源选择、库存遍历和封存仍可能进行文件 IO，这次解除的是原生解析／探测持有 case 写事务的问题；累计预算和大文件 IO 的完整政策继续 N4-05。

## 资源选择与历史兼容

Markdown 解析使用锁内捕获的原字节，通过所属 helper 的真实 stdin 交给 Pandoc。解析后的图片／HTML 资源先读取并校验所选字节，再在提交锁内重验；工具解析本身不会读取活动算例的图片文件，正式编译仍在封存副本上进行。

新报告的 source 保存资源候选路径和实际选中路径。例如 `plot.svg` 原先在算例根目录，`E-report/plot.svg` 不存在；准备后新增后者，即使原图没有变化，也必须报冲突。仅比较原图哈希会漏掉这种查找优先级变化。发布前独立解析重算同一记录，读取当前来源时不在 case 锁内再次启动 Pandoc。

旧报告 descriptor 不含资源查找记录时，继续使用原有来源结构验证；不会为历史版本臆造解析结果。LaTeX 则重新计算动态源文件包，新增／删除符合规则的资源也能使准备失效。无效 Markdown、SVG、LaTeX 入口或工程条件仍不能通过提前创建 Job 来承载失败。

## HTTP 与工作流取消

HTTP 准备操作有独立的 ASGI receive 等待者，在请求体已解析后等待真实 http.disconnect；对于不使用 body 的报告入口，先消费剩余请求体。断开后取消准备任务，所属执行器清理原生范围、等待同步线程退出并保存结果，再结束请求。原先用 `Request.is_disconnected()` 的非阻塞轮询在本项目中间件组合下可能漏掉断线，并使快速 compose 准备的监视任务收尾等待；该实现已移除。

工作流 dispatch 先持久保存可查询的 run／ready step，再释放写事务完成报告准备。准备期间用独立短只读会话观察 run／step 的版本和状态；取消或状态变化会停止这次准备。回到锁内再次核验版本、ready 条件及工程输入，随后才能创建 attempt。确认阶段已经提交的工程状态不会因后续准备失败被撤销；失败后仍需用户显式重试或新建工作流，不会自动换用另一个已确认生产者。

创建／dispatch 端点和 caseDict 工程确认后的 dispatch 都传递 HTTP 请求归属。已有 queued attempt 的重派发继续使用原子 claim 防重，不创建第二次原生作业。跨重启恢复、孤儿准备目录及请求历史保留仍由 N5／N4-05 后续处理。

## 验证与剩余入口

`document_admission_smoke --postgres` 使用正式 `/report:build`、`/latex:build`、真实 Pandoc 和 WSL 工具，故障只注入临时 app；工具暂停时另一个正式文件写入必须先完成。报告覆盖正常、MD／revision／资源改写、同名候选新增、同键双提交、错误出生身份、登记／停止回执失败、超时、真实 HTTP 断线和 owner 猝死；LaTeX 覆盖正常、源变化、真实 HTTP 断线和 owner 猝死。失败或失效均没有新 Job；已提交请求在源文件删除后仍可重放。

工程报告专项另覆盖准备期间工作流取消：真实 Pandoc 停止、请求记录 cancelled 且 cleanup_verified、report step 没有 attempt，随后显式创建新工作流可以完成发布。完整通过数量、最终运行路径和修复前失败来源均记录在 `tmp/mainline-N4-admission-evidence/`，以实际汇总为准。

继续推进 runtime／CLI 探测、SyncTeX 请求、PTY／viz 会话和 Agent／CLI；仍需补齐其余专业入口的原生归属、全部取消／通知竞争、预算／公平队列／停机和 N5 恢复。准备请求现已响应HTTP断线和所属工作流变化，统一活动列表、算例级取消与停机还须接入这一类范围。本次未关闭整个 N4-03、N4-04 或 295 项功能中的整项交付。

实现：[两阶段协调](../server/app/services/document_admission.py)、[报告处理器](../server/app/services/report_handler.py)、[LaTeX 处理器](../server/app/services/latex_handler.py)、[正式入口](../server/app/routers/reports.py)、[工作流 dispatch](../server/app/services/workflow_execution.py)、[专项](../server/document_admission_smoke.py)。
