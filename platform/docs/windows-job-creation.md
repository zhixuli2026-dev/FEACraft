# Windows 宿主在创建时加入 Job

2026-11-07。已修复 `OwnedProcess` 创建宿主后、读取 HELLO 再分配 Job 的空窗。现在所有 Windows 宿主创建路径通过 `PROC_THREAD_ATTRIBUTE_JOB_LIST` 在系统创建调用内加入原有 Job，之后才向异步执行器返回进程；准备阶段的 `created` 回调已能核验真实 Job 归属。没有在失败时回退到原来的先创建再分配方式。

这项修复处理宿主创建边界，不代表“没有出生观察”的历史记录已经可以恢复，也没有替代 WSL 原生范围。主线保持 **33/53 完成、20 项未完成；资源块 11/17 完成、6 块未完成；295 项全功能最终验收继续**。

## 实现及依据

微软说明 Job 列表属性在新进程初始线程运行前完成分配，并指出“先以挂起状态创建、再加入 Job”仍会在所有者中途退出时遗留孤儿进程。因此没有采用挂起后分配作为替代方案。[创建时指定 Job](https://devblogs.microsoft.com/oldnewthing/20230209-00/?p=107812/)；[属性与继承句柄要求](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute)。

新增 `app/services/windows_process_create.py`：

- 调用实际 `CreateProcessW`，同时指定 Job 列表与仅包含三个标准输入输出管道的继承句柄列表。Job 句柄不交给子进程，保留最后所有者句柄关闭时终止成员的行为。
- 隐藏控制台；保留 Unicode 参数、环境和工作目录；参数仍作为 argv 编码，不增加 shell 解释。拒绝不支持的启动选项、关闭的 Job 和不完整标准句柄。
- 保留本机 CPython 的重叠管道及 Proactor 进程等待，使用局部 Popen/transport 子类接入，不修改全局 subprocess。当前实际验证环境为 Windows、Anaconda CPython 3.11.7；这层使用 CPython 内部接口，后续升级 Python 时必须重新验证，不能假定未经验证的版本兼容。
- `OwnedProcess` 在保留实际创建句柄后立即核验 Job 成员关系；HELLO 时再次比对出生身份及归属，替代原来的事后分配。正常停止仍使用原有真实句柄、Job 全生命周期观察和 WSL 出生观察，未放宽退出凭证。

## 实际验收

[最终 Windows 入口证据](../../tmp/windows-job-create-bc5d1358a6f74263aab8e2bf9ba09f35/result.json)绑定本次执行源码，5 组实际基础设施场景通过：

1. 子进程最早的测试逻辑已经在目标 Job 中；Unicode 路径/参数/环境、工作目录及 128 KiB 原样双向管道数据通过。
2. 内核 Job 活动进程限制为零时，实际创建被拒绝，负载文件未生成，无未归属启动回退。
3. 调用真实 CreateProcess 后，在 Popen/asyncio 接管句柄前让实际所有者 `os._exit(81)`。此前已经核验创建句柄属于目标 Job；之后该出生身份实际退出，独立作用域外的进程仍存活。
4. 在生产 `created` 回调实际取消准备任务，回调时成员关系已成立；未放行负载不执行，实际宿主退出。
5. 生产 prepare/登记验证/release/输出/完成路径通过；正常结束凭证保留完整 Job 生命周期观察，3 个成员全部有退出证据。

最终检查的 6 个记录出生身份均退出。前一版仅验证适配器的 3 组证据保留在 `windows-job-create-0d472233f3714df580b8702c310eb54d`，不把它当作接入后的完整验证。

[PostgreSQL 准备取消与重试](../../tmp/sandbox-recovery-ui-retirement-oiyp7pli/image-live-owner-checks.json)：3 项基础设施、1 项回执篡改故障通过，已知准备取消后准确释放节点预留，存活 API 不误阻止终止请求重试；[临时数据库已删除](../../tmp/image-postgres-022h0c9k/result.json)，9 个登记出生身份实际退出。

[SQLite 启动阶段实际退出](../../tmp/sandbox-recovery-ui-retirement-rlr67p2a/image-preparation-checks.json)：在 `created` 和 `native_ready` 提交后真正结束 API，实际宿主及已知 WSL 门退出，再启动服务并明确重试；3 项基础设施、1 项持久观察篡改故障通过。未知 identity/stop 原日志保留，没有伪造正常登记；9 个完整登记身份退出，准备阶段部分出生观察另存于专项结果。

上述两个完整链路各自完成真实模型工具读取恢复文件：

- [PostgreSQL 真实调用](../../tmp/sandbox-recovery-ui-retirement-oiyp7pli/live-result.json)：2 次、8391 tokens。
- [SQLite 真实调用](../../tmp/sandbox-recovery-ui-retirement-rlr67p2a/live-result.json)：2 次、8724 tokens。

本轮共 **4 次真实 `api.deepseek.com` / `deepseek-flash` 调用，17115 tokens，HTTP 均 200，模拟 LLM 为 0**。实际文件工具选择、随机恢复内容读取、流式回复与持久记录通过；两组严格仅回复凭证字符串均仍为 false。实际基础设施与协议故障检查没有用模型模拟。开发数据库、schema、前端和认证配置未修改。

## 接下来仍需完成

继续[原生作用域接入计划](native-cgroup-startup.md)的持久意图、受控控制器、执行器传播与独立恢复凭证。必须将本次原子宿主创建实现、精确 Job/nonce 和 WSL boot/inode 证据绑定到持久记录，处理作用域创建成功而提交中断，并覆盖撤销后迟到加入、双 API/双库竞争与实际中断。

本次没有赋予历史无出生身份记录新的清理权限；`host_ready` 后未记录 WSL 出生的窗口仍然拒绝。完整恢复还需 Windows/WSL 联合范围核验，不能仅凭所有者退出或 Linux 作用域不存在放行。原生 unlink/回执故障、权限/WSL及主机重启/空间耗尽、其他副本和长期快照额度、目录/权限/删除/caseDict/大批量/三方冲突，以及 RB-12 至 RB-17 和其他主线/全功能要求保持。
