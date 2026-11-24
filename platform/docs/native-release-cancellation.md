# 原生子执行与准备请求的最终放行检查

2026-11-03，N4-04。继 [通用 Job 放行顺序](job-release-cancellation.md) 后，继续修复 `NativeExecutor` 的独立子执行边界。注册报告/LaTeX 的 worker、发布前内容复验，以及不创建 Job 的原生准备请求，具有不同的持久记录，不能仅由父 Job 的修复推断它们已经受到保护。

## 复现与原因

两个隔离 API 都在创建作业前启动。报告 worker 发起真实 `pandoc_parse` 调用后，测试在子执行登记已提交、最终放行尚未发送的位置暂停；第二个 API 提交父 Job 取消，查询确认 cancelled，而原生载荷标记尚不存在。

解除暂停后，标记包装器仍被启动。包装器随后调用原 Pandoc helper 的 argv；此处证据只证明原生载荷在取消提交后仍被放行，不声称修复前编译成功。原作业最终保持 cancelled，捕获的子执行也被实际清理。证据：`tmp/child-release-before-xfjpofdm/before-result.json`。

原因是原路径虽在子执行登记时检查了父 Job 状态，登记后的最后一次检查只读取进程内的停止标记。另一个 API 的持久取消可能尚未通过通知或心跳到达本进程。

## 实现

`NativeExecutor` 的最终放行现在必须经其对应的 `records.release`：

| 持久记录 | 放行前的事务内检查 |
|---|---|
| `native_child_records` | 与父 Job 取消共用 case 控制锁；当前 claim/worker/父启动 nonce、running/finalizing 阶段、取消请求、租约、子执行 running 登记及完整实际身份绑定 |
| `native_request_records` | 持有该请求记录的更新锁；当前 owner/token/nonce、active 阶段、未过期租约、子执行 running 登记及实际身份相同 |

持有记录锁期间，只调用最长 5 秒的最终控制发送并完成事务。实际宿主/WSL 身份探测在锁外；已有 Agent 授权回调仍在原位置执行。发送回调在真正调用进程放行前再次检查进程内停止标记，使本地关闭不会因等待数据库锁而丢失。

取消先提交时，不发送子执行放行；子执行先取得锁时，父 Job 的取消须等待该事务完成，随后按已启动执行处理。租约在最终检查时已经过期，同样拒绝放行。控制消息已经发送、后续事务提交失败时，载荷可能已经运行；原 `finally` 路径等待实际所属范围退出并持久保存失败和停止回执，不假定没有副作用，不自动再执行。

拒绝放行的子执行仍有真实登记和清理事实。其 journal 可为 failed，同时 `stop.verified=true`；前者表示调用没有正常完成，后者表示本次范围的退出已经核验。已提交的父 Job cancelled 不会被子执行错误覆盖为成功。

本次不新增持久取消字段或 HTTP 取消接口，也不将 NativeRequest 的 active/owner 检查描述为新的跨实例用户取消协议。PTY/viz/CLI 的会话关闭、Agent 权限撤销与命令放行的完整竞争、真实 Redis 通知和 N5 所有者恢复仍分别推进。

## 验证与范围

`native_release_smoke.py --postgres` 的完整矩阵在隔离 SQLite / PostgreSQL 中各包含 20 项：

- Pandoc、LaTeX 各 5 项：正常 worker、worker 放行前父取消、finalizer 放行前父取消、worker 租约过期、finalizer 放行后提交失败。
- Pandoc worker、LaTeX finalizer 各 1 项：子执行先持有放行锁，另一个 API 取消等待该边界；通知发送故障由夹具注入。
- Pandoc、LaTeX 准备请求各 4 项：正常、登记后租约过期、放行后提交失败、本地执行器关闭。

测试标记包装器运行原 helper argv：Windows 显式等待原 helper 子进程，Linux 使用 exec；正常场景仍执行真实 Pandoc/LaTeX helper。取消先提交、过期和本地关闭场景要求标记不存在且控制消息未发送。每项检查实际宿主出生身份退出，WSL 场景另重新探测 Linux 出生身份；原始源码 SHA 不变，外部哨兵存活。Job 子执行由对端 API 读取相同持久 journal。

这些检查没有使用真实 Redis/arq 故障环境，没有增加新的取消时延承诺，也不证明任意外部服务或用户脚本跨运行时后代已经隔离。准备请求的本地关闭不等于新增持久跨实例取消功能。

第一轮夹具在创建账户前调用受保护的重置接口，得到 401，尚未启动测试作业。第二轮 Windows 标记包装器使用 exec，包装器提前退出导致真实 helper 被所属范围收尾，正常解析得到空输出；改为显式等待原 helper 后重跑。两次失败日志与最终结果分别保存，不作为产品竞争缺陷的复现证据。

第三轮先通过 11 项，随后 LaTeX finalizer 的“放行先取得锁”场景已发送控制消息，但后续取消在载荷获 CPU 前结束了进程，实际退出码 143、停止回执 verified，标记没有出现。这是有效取消；原测试错误地要求放行后载荷必定运行。修正为核对控制消息已发送、取消顺序和实际退出，并保留载荷是否运行的观测值。SQLite 保留先前逐项通过且源码未变的 11 份回执，另复验该场景及尚未运行的 8 项；最终汇总明确标为 11+9，而非一次完整运行。PostgreSQL 独立执行完整 20 项，产品代码在这些运行间没有变化。

正式 Pandoc、LaTeX、原生准备、文档准备、Agent shell、runtime、SyncTeX 及通用 Job 放行共 8 套兼容回归的实际结果另存 `tmp/mainline-N4-native-release-evidence/`。原始路径和范围见 `evidence-origin.json`。源码基线：`platform/backups/mainline-N4-native-release-20261103.zip`，SHA 及逐文件匹配另存；没有恢复后重跑。

本批无 DDL、迁移、UI 或开发服务变更。主线 53 项和全功能 295 项的范围不变，N4-03/N4-04 继续保持实施中。

```powershell
platform/server/.venv/Scripts/python.exe -X utf8 -B platform/server/native_release_smoke.py --postgres
```
