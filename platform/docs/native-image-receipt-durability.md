# 原生镜像删除与回执目录同步

2026-11-07。`sandbox_image_cleanup_native.prepare` 在完成回执文件写入、fsync 和重命名后，再打开回执所在目录并执行 fsync，成功后才允许内部校验进程继续。目录打开使用 `O_DIRECTORY | O_NOFOLLOW`，异常直接使请求失败；仅能读取到 `complete.json` 不代表整个请求已经完成。

## 实际故障矩阵

显式入口为 `live_llm_image_native_fault_acceptance.py --source-profile-id 26 [--postgres]`。测试复制 Python/固定 shell 辅助代码，在副本中插入定点故障，记录全部原始与副本哈希，生产代码没有测试开关。只有副本的清理函数与回执写入函数插入故障；所有身份、范围控制、数据库、镜像删除和回执字节均实际执行。副本保留 `app/services` 固定路径结构。

| 故障位置 | 镜像状态 | 回执状态 | 请求结果 |
| --- | --- | --- | --- |
| unlink 前实际退出 | 原镜像保留 | 无回执 | 失败 |
| unlink 后、父目录同步前实际退出 | 镜像已删除 | 无回执 | 失败 |
| 回执重命名前实际退出 | 镜像不存在 | 仅 pending 文件 | 失败 |
| 回执重命名后实际退出 | 镜像不存在 | complete.json 存在 | 失败 |
| 目录同步位置注入 OSError(EIO) | 镜像不存在 | complete.json 存在 | 失败 |
| 目录同步成功后实际退出 | 镜像不存在 | complete.json 存在 | 失败 |

每次实际核对注册原生身份、宿主/原生退出、正常 stop 与范围撤销；API 所有者仍活着，GET 不改写失败记录，管理操作继续拒绝。最后新请求重试成功，仅一个完成事件，原始失败记录不改写，`deleted_this_request=false`，不能冒认本次重试执行了此前的删除。快照内容保持不变，实际模型从快照恢复的文件中选择工具读取随机验证串。

矩阵共用一个隔离镜像，第二个位置实际删除它；后四个位置验证“镜像已不存在”的回执发布与重试，不声称它们分别执行了另一次物理删除。EIO 是明确标注的协议故障注入，不是实际硬盘故障。五处退出使用 `os._exit(82)`，是实际 Linux 进程退出。

## 结果与证据

- SQLite：[故障矩阵](../../tmp/sandbox-recovery-ui-retirement-aeemx72_/native-image-fault-checks.json)、[真实模型](../../tmp/sandbox-recovery-ui-retirement-aeemx72_/live-result.json)。6 项实际基础设施检查、1 项协议故障、1 项真实模型读回检查通过；2 次 DeepSeek 调用，3,302 tokens。此初版副本没有复制内置技能资产，提示词目录比生产路径少，不能据此证明完整生产提示词一致；后续副本已补齐静态技能文件。
- PostgreSQL：[故障矩阵](../../tmp/sandbox-recovery-ui-retirement-q687jalh/native-image-fault-checks.json)、[真实模型](../../tmp/sandbox-recovery-ui-retirement-q687jalh/live-result.json)、[临时数据库清理](../../tmp/image-postgres-f80ecw64/result.json)。同样 6 项基础设施、1 项协议故障、1 项真实模型读回检查通过；2 次调用，8,338 tokens。副本包含内置技能资产及其哈希。
- 未注入故障的生产路径：[正常清理与实际锁/loop 拒绝](../../tmp/sandbox-recovery-ui-retirement-53iaiek9/image-cleanup-checks.json)、[真实模型](../../tmp/sandbox-recovery-ui-retirement-53iaiek9/live-result.json)、[进程退出](../../tmp/sandbox-recovery-ui-retirement-53iaiek9/cleanup.json)。4 项基础设施、3 项协议故障和 1 项真实模型检查通过；2 次调用，8,479 tokens，15 个登记身份已退出。

以上三套合计 16 项实际基础设施检查、5 项协议故障、3 项真实模型读回检查；6 次真实 DeepSeek 调用、20,119 tokens，模拟 0。模型成功读回并保留工具/消息事件，但这三套都附带额外文本，严格纯字符串格式未通过；后续输出约束修复另行验收，不改写这些历史结果。

## 保留的失败

- 两次隔离副本启动失败：[te0qx9l9](../../tmp/sandbox-recovery-ui-retirement-te0qx9l9/result.json)、[dwth91kz](../../tmp/sandbox-recovery-ui-retirement-dwth91kz/result.json)。初版既缺少固定 shell 脚本，也未保留 `app/services` 路径；两项均已修正，未分别隔离证明首次异常的唯一原因。没有放宽生产启动校验，两次均未执行模型推理。
- 正常路径首次在清理 POST 超时：[原始退出核验](../../tmp/sandbox-recovery-ui-retirement-exj7chjc/cleanup.json)保留七条 `native_probe_unavailable`；[后续只读复查](../../tmp/sandbox-recovery-ui-retirement-exj7chjc/cleanup-recheck.json)确认全部 13 个原身份已退出。没有用后续结果覆盖初次失败，也未据此认定当时的资源/时延问题已修复。
- 正常路径第二次在辅助锁进程的握手文件解析失败：[算例结果](../../tmp/sandbox-recovery-ui-retirement-qov2fc89/result.json)。父进程可能在文件创建但 JSON 尚未写入时读取；`sandbox_image_guards.py` 改为完整写入并 fsync 临时文件后原子重命名，再次实际运行通过。该修复属于测试握手，不是模型模拟。

## 剩余范围

本轮验证实际进程中断和故障传播，没有进行宿主断电、宿主重启或 WSL 重启，也不证明所有上级目录及卷的断电持久性。准备/许可边界双 API 竞争、迟到加入、原生及守护期限、目录/权限/实现升级/空间故障、整个请求的统一资源预算继续；上阶段偶发退出核验拒绝仍未修复。

主线仍为 33/53 完成、剩余 20 项，资源 11/17 完成、剩余 6 块；295 项全功能最终验收继续。参见[主线计划](next-stage-mainline-plan.md)与[资源清单](resource-budget-worklist.md)。
