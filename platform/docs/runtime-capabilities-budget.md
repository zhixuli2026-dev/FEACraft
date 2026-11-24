# 运行环境自检的并发与完成期限

2026-11-21，RB-14 的运行时监控和指标复用两路存储探测准入。确认管理员身份后先释放数据库事务，再到工作线程执行存储探测。监控沿用配置中的存储测量期限。到期或断开后，已开始的 I/O 由原请求等待结束，清理各自的探测文件后归还名额。一条请求完成后保留共用的空目录供其他探测使用。

实际 Windows 文件系统和 SQLite 验收中，监控与指标请求各写入一个真实探测文件，并暂停在写入后。两秒期限到达后，两条请求等待各自线程，第三条探测返回 429。其间健康接口可用，独立账户写入数据库成功。释放文件 I/O 后两条请求均返回 504，两个临时文件已清理。再次查询监控和指标均成功。完整自检的原生停止和故障降级专项也通过，十条探测记录均已终结，API 进程实际退出。真实和模拟模型调用均为零。[监控与指标验收](../../tmp/runtime-capabilities-budget-ah1egh0y/runtime-capabilities-budget.json)

2026-11-19，N4-05 / RB-13。真实 `/api/v1/runtime/capabilities` 在两个请求运行期间已经创建四条原生探测记录。第三个请求等待 3.297 秒并创建两条额外记录。隔离 API 结束后，六条记录所属的宿主及已登记的 WSL 进程均观察到退出，五个已写出身份的实际后代也退出。原库仍保存当时的 `active` 记录。[修复前复现](../../tmp/runtime-capabilities-budget-79_jjz66/runtime-capabilities-budget.json) · [独立进程退出观察](../../tmp/runtime-capabilities-budget-79_jjz66/baseline-exit-verification.json)

修复后每个 API 进程同时接受两个自检请求，分别启动已归属的 Python 和 WSL 探测。容量用满即返回 `429 runtime_capabilities_busy` 和 `Retry-After: 1`。已接收请求在认证依赖完成后共用 65 秒单调截止时间，沿用原 Python 等待的最大时长，超期返回 `504 runtime_capabilities_timeout`。断线或超时触发现有停止流程，原生进程和正在运行的存储探测处理完毕、所属记录收尾后才归还名额。对于已启动的同步 I/O，调用可能在期限后等待线程收尾再返回。[公开入口实现](../server/app/routers/runtime.py) · [原生探测归属](owned-runtime-probes.md)

最终受审套件 1/1 通过，使用真实 Windows、`vibeflow-linux`、独立 API、SQLite 和文件存储。匿名自检返回 401。第三个并发请求用时 0.297 秒并返回 429，数据库仍为四条原生探测记录。断开原两个连接后，暂停最终数据库提交时仍返回 429。放开提交后四条记录均以 `cancelled` 终结并持有已核验的停止回执。夹具将总期限缩短到 8 秒，实际 HTTP 返回 504 总用时 9.531 秒，两条探测以 `TimeoutError` 终结，停止后才返回。四次断线停止及两次超时停止均独立确认实际 Windows、WSL 及已标记后代退出。[验收结果](../../tmp/runtime-capabilities-budget-21ssls9q/runtime-capabilities-budget.json) · [执行器摘要](../../tmp/vibeflow-regression-all-20261119-124912-dca6d0/summary.json)

真实 Python 进程以退出码 49 结束、探测目录被普通文件占用时，公开响应按原格式分别报告 `ChildExecutionError` 和 `FileExistsError`。两处恢复后同一入口返回 200，观察到 Python 3.11.7、WSL OpenFOAM v2406、数据库与文件后端健康、CLI 路径和详情一致。全部十条原生请求终结，隔离 API 的实际出生身份核验退出。真实和模拟模型调用均为 0 次。[健康和故障回执](../../tmp/runtime-capabilities-budget-21ssls9q/runtime-capabilities-budget.json)

FF-G13 的浏览器证据沿用既有 Edge 验收。管理员保存真实解释器、缺失 bashrc 或未安装发行版后刷新，页面展示对应系统状态。恢复默认配置后重新探测恢复可用，普通成员的设置写入为 403。CLI 字段报告路径发现结果。[FF-G13 原审计](feature-gap-audit.md) · [浏览器流程](FF-G09-runtime-setup.md) · [Edge 回执](../../tmp/runtime-setup-browser-fk_i8eba/runtime-setup-result.json)

此前的 Windows 和 Linux 真实 ParaView 部署也保存了同一公开自检的响应。两个响应均记录 S3 存储探测成功、具体耗时和 A–E/V 六个阶段检查。[Windows S3 回执](../../tmp/windows-paraview-rrf2tczz/capabilities.json) · [Linux S3 回执](../../tmp/linux-paraview-20261118/linux-v3/capabilities.json)

首轮修复后运行通过 429、实际断开、504 和故障降级检查。验收最后把公开健康字段 `database` 写成 `db`，读取正常响应时失败。第二轮完整业务检查通过，夹具在 Windows 虚拟环境启动器退出后立即观察 API 子进程，早于该子进程实际退出。随后按同一出生身份独立确认退出。两次均保留失败记录，最终复验在至多 12 秒内持续核对该真实身份。[首轮](../../tmp/runtime-capabilities-budget-d9lj9w2u/runtime-capabilities-budget.json) · [第二轮](../../tmp/runtime-capabilities-budget-uquf6me6/runtime-capabilities-budget.json) · [独立 API 退出观察](../../tmp/runtime-capabilities-budget-uquf6me6/independent-api-exit.json)

本轮验证单 API 进程内的公开自检。原 RB-13 的其他独立 native 与 CLI 入口生命周期继续按原任务核对。C 盘可用空间从 22.22 GiB 变为 22.21 GiB，隔离文件和回执位于 D 盘，本轮未运行 Docker 操作。
