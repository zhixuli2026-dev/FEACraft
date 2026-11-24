# 固定只读身份探测的所属范围与原生期限

2026-11-03，N4-03.h。先前进程、CLI bridge 和 PTY gate 的身份检查直接在 API 线程中调用 `subprocess.run(wsl.exe, timeout=10)`。固定程序只读 `/proc`，但该超时及所属 API 的消失不能保证 Linux 探测进程退出。

## 复现与修复

隔离 API 使用原身份探测程序的私有副本，在实际读取前插入最长 30 秒的测试暂停。API 真正退出后，按 Linux 出生身份重新读取，探测仍存活。测试随后通过只属于该夹具的释放文件结束它，并再次验证退出。证据保留在 `tmp/control-reader-before-svnf1_bv/before-result.json`；没有把人工释放算成产品清理成功。

现在 `OwnedProcess.probe_native`、`OwnedStdio.native_bridge` 和 `OwnedTerminal` 的两处 ancestry 检查统一使用 `control_identity.ControlRead`：

1. 只接受身份读取或父子关系映射及严格的 Linux 身份结构。发行版、请求大小、固定帮助程序的 SHA 均校验；接口不接受命令、脚本路径或 shell 片段。
2. 使用既有 `OwnedProcess` 的 Windows 启动门和保留句柄，把固定 host reader 及它创建的 `wsl.exe` 放入独立 Windows Job。只读基础设施先核验本次宿主范围再放行，不递归调用自身的原生身份探测。
3. host reader 先列出运行中的发行版，缺少目标时返回不可核验，不主动为历史查询启动已停止的发行版。列表和启动之间仍存在系统级状态变化窗口，这不是发行版状态的原子锁。
4. 固定 Linux reader 安装默认终止动作的 `SIGALRM`，期限为 10 秒，并用独立线程观察仅由 host reader 持有的 stdin 管道。管道关闭或收到意外字节时只退出自身。它不运行外部命令、不创建子进程，也不凭被查询的 PID 发信号。
5. Windows 宿主保持管道打开，直到实际 reader 结束；读取 stdout/stderr 有上限。API 等待宿主范围退出并核验停止回执，再返回严格校验过的结果。取消会先清理本次读范围；异常结果不会被当作身份匹配。

这是启动门所需的固定只读基础设施，不是业务执行入口，不创建 Job 或数据库执行行。其实际所属句柄、读请求 nonce 和代码摘要仅用于本次调用；查询对象的身份仍只是证据，不能赋予终止它的权限。没有放宽原工程作业、PTY 或 CLI 的持久登记后放行规则。

原生 10 秒期限从 reader 安装计时器开始。宿主发行版列表最多 10 秒，原生客户端等待最多 15 秒，API 侧结果接收最多 25 秒，另有既有启动门和清理期限。不能把它表述为“整个请求必定 10 秒完成”；统一准备/运行/收尾预算仍由 N4-05 处理。

## 验证

`control_identity_smoke.py` 的 11 项真实 WSL 验证覆盖：正常身份、实际父子关系、出生标记不匹配、非所属 ancestry、目标发行版未运行、选择后代码变更、错误响应 nonce、输出超限、取消、原生超时及真实 API owner 猝死。

正常响应还核对 reader 自己的出生身份；除 API 猝死外，已创建范围均检查完整 Windows 成员回执及宿主退出。全部场景逐项重新探测捕获的 Linux reader 已退出，独立原生父子哨兵始终存活。取消和 API 猝死要求从触发至退出观察小于 4 秒，独立于 10 秒期限；超时场景要求 8–15 秒内结束。API 猝死没有伪造尚未持久保存的完成回执。

测试延迟仅存在于私有 helper 副本，插入点在真实期限与管道监听安装之后；产品没有测试模式或可注入脚本路径。首轮测试控制模块因换行转义导致导入失败，修正后通过；日志仍保留。

终端兼容测试还修正了原有时序假设：运行期心跳失败以前从创建会话起就自动触发，WSL 启动超过首次心跳时刻时会先拒绝启动。现在夹具在收到 ready 后明确启用该故障，保持运行期验证的原意；没有为了通过测试延长产品心跳或租约。

规范证据在 `tmp/mainline-N4-control-readers-evidence/`；原始路径、失败日志、计时、双库和 CLI 兼容结果见 `evidence-origin.json`。源码基线为 `platform/backups/mainline-N4-control-readers-20261103.zip`，摘要见同名 `.sha256`，逐文件匹配另存证据目录。没有在恢复目录重新执行。

```powershell
platform/server/.venv/Scripts/python.exe -X utf8 -B platform/server/control_identity_smoke.py
platform/server/.venv/Scripts/python.exe -X utf8 -B platform/server/process_registration_smoke.py --postgres
platform/server/.venv/Scripts/python.exe -X utf8 -B platform/server/terminal_execution_smoke.py --postgres
```

本批无 API 响应字段、数据库 schema 或 UI 修改；全部测试使用一次性 API、数据库和存储，实际 CLI 使用既有本地 Responses 夹具。开发库、端口 8000、现有用户终端及远端推理未动。

## 后续边界

固定身份控制过程的生命周期缺口已补。旧 report worker 的正式调用者、LaTeX/Pandoc 独立工具回退、专业入口剩余竞争与持久读取仍须逐项核对。任意用户脚本创建的跨运行时或远程任务不在本只读通道之内。N4-04 持久取消与发布竞争、N4-05 全程预算、N4-06 公平调度、N4-07 活动与停机以及 N5 恢复保持各自验收范围；53/295 台账不因该修复完成整项。
