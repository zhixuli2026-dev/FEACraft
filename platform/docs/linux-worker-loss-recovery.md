# Linux worker 运行中失联恢复

2026-11-16，N4-07 阶段实现；主线仍为 48/53。

## 行为和实现

此前普通 Linux worker 命令已经由固定 gate 放入独立 PID 命名空间。worker 硬退出后，管道断开会触发停止，命名空间 init 退出会清理其后代；但原数据库记录只有外层 gate 的身份，不能据此证明完整执行范围已停止。

现在复用同一个命名空间，在业务命令启动前增加持久化登记：固定 PID 1 等待放行，外层 gate 从宿主 `/proc` 核验其真实进程身份、独立命名空间和 `NSpid=1`，worker 将其写入原 process identity 的 `namespace_init` 以及 `attempt.linux_namespace_prepared` 事件，提交后才放行业务命令。宿主原生子执行也接入这一登记。Windows/WSL 原协议不增加该字段。

执行主机上的原有恢复扫描在确认原 worker、全部登记 gate 和 namespace init 的真实身份都已退出后，写入原 v4 类型的恢复排空 checkpoint，使用 Linux namespace 证据类型。校验要求每个已登记根/子执行均有对应命名空间事件，身份与原始 process identity 完全一致。缺失登记、未知身份、尚存进程或不完整子执行仍拒绝恢复。

原任务保留 `interrupted/recovery_required`，不重放命令，不伪造原 worker 的成功退出码或停止回调。原始 `stop` 仍为 null，child journal 不改写；失败的发布预留结束，之前成功发布的产物保持原样。完成收尾核验后，用户可提交新的任务。

## 实际验收

显式入口 `platform/server/live_llm_remote_namespace_acceptance.py` 使用实际 PostgreSQL、S3、Redis、Windows API 和 UID 65534 的 Linux worker。任务启动一个脱离原进程组、忽略 SIGTERM、持续写入随机测试路径的后台进程，随后让实际 worker 执行 `os._exit(17)`。

通过的关键观察包括：

- `/proc` 中实际记录了 4 个命名空间成员：init、shell、业务 Python 和后台写入进程。
- 原 worker 确实退出；新 worker 自动完成原任务的中断与恢复排空。4 个原始出生身份均退出，后台文件停止变化。
- 原任务仍只有一个 attempt，原输入、worker/进程身份、child journal 和发布身份不变；缺失的原始停止记录仍缺失。恢复事件仅一条且 `replayed=false`。
- 上一次成功发布的文件保持不变；新提交的任务成功发布新的随机证明，真实 DeepSeek 通过 read_text_file 读回。
- 对真实 SQL checkpoint 的内存副本做 8 种纯校验破坏（缺失命名空间、改变 init 出生身份、伪造停止、仍存活 owner/init 等），全部拒绝；不修改数据库，也不模拟模型。

最终远端证据在 `tmp/remote-worker-14gjsnsy/`，数据库清理和临时访问规则恢复证据在 `tmp/remote-worker-postgres-5a446d3ad9d142a591cacd475987850a/`。最终默认 Windows/WSL 兼容回归在 `tmp/worker-shutdown-84xx0cmu/`。实际通过数、模型调用数和源散列以本阶段 `validation.json` 为准。

首轮 `remote-worker-arzfk87k` 的验收器错误等待 S3 的实时日志，而该日志在收尾时才可读；实际写入进程已启动。改为读取持久运行身份及真实变化文件后，`remote-worker-pzi84b84` 通过恢复，但模型请求被消息写入前的 `storage_quota_busy` 拒绝，真实模型调用为 0。已保存结构化 409 内容，验收器仅对此明确的准入拒绝做有上限的重试，记录实际拒绝次数；不重试已接受的 SSE，也不替代模型回复。该次原因不用于推断更早未保存响应体的 409。

## 尚未证明的范围

本轮完整验证普通宿主命令的运行中失联与新任务恢复。专业 handler 的跨系统路径/工具环境映射、其子执行组合的实际故障验收，以及未完成登记的启动中失联仍按原计划继续。已存在历史记录不会凭空补出 namespace init 身份。N4-07 整项、N4-05/N7 剩余事项和 295 项全功能目标不因本轮而关闭；不新增权限或隔离层。
