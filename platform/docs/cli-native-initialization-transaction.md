# Linux 私有状态的可恢复初始化

2026-11-08，接续 [节点残片恢复](cli-node-fragment-recovery.md)。原初始化器先创建正式 home，再写 config 和 owner；中断后正式目录已存在，后续初始化会永久失败。本阶段改为在冻结输入绑定的暂存目录完成文件写入，再无覆盖发布。

## 写入和恢复

`codex_wsl_state.initialize` 仍只使用 Linux 标准库、以 UID 65534 执行。账户目录使用零字节 `.initialize.lock` 和有界 flock 等待，协调多个真实初始化进程。暂存目录名包含规范化请求的 SHA-256，绑定根、账户路径、配置和 owner；不同输入不能自动接管同一账户的残片。

初始化先验证所有已存在残片、文件类型、单硬链接、UID、0600 文件权限及 0700 目录权限。仅当残片字节是冻结输入的完整前缀时，才在原文件尾部续写；不截断或重写未知内容。叶文件用 O_NOFOLLOW 打开，符号链接、硬链接和不匹配数据拒绝处理。owner 编码改为规范键序，消除不同进程 JSON 键顺序带来的前缀差异；已完成的旧 owner 仍按 JSON 内容验证并保留原字节。

两份文件完成并 fsync 后，以 Linux renameat2(RENAME_NOREPLACE) 先发布 owner，再发布完整 home，随后移除已空的暂存目录并创建 tmp。文件和目录发布都不覆盖既有目标；每个发布边界后可按同一冻结输入重试。锁文件为零字节、数据通过 rename 移动而非复制，所以初始化逻辑字节上限仍是 config+owner，现有 user/node 预留和数据库约束不变。目录元数据、磁盘分配块和 inode 并非这个逻辑字节计数的 OS 硬配额。

已有正式 home 缺少合法 owner、存在未知暂存内容、旧残缺目录没有匹配操作凭据时保留并拒绝自动重建。此恢复能力不意味着可以覆盖历史未知用户数据。进程中断与有限目录同步已验证，宿主断电及所有祖先目录的持久性不在本次保证内。

## 原生进程与空间故障验收

显式入口 `live_llm_cli_native_initialization_acceptance.py --source-profile-id 26 [--postgres --s3]`。Linux 检查直接运行生产初始化器，使用实际进程、SIGKILL、文件锁和文件系统；不是模拟模型或 CLI 推理。

| 真实 SIGKILL 位置 | 后续验证 |
| --- | --- |
| 已创建暂存目录 | 两个独立初始化进程并发重试成功 |
| owner 写入一半 | 原前缀续写，完整 owner/config 校验通过 |
| owner 已完成并同步 | 重试完成，不重复复制数据 |
| config 在 UTF-8 多字节中间中断 | 按原始字节前缀恢复，不依赖残片能解码 |
| config 已完成并同步 | 重试无覆盖发布 |
| owner 已发布、home 尚未发布 | 完成同一暂存 home 的发布 |
| home 已发布、暂存目录尚未清理 | 验证正式文件，清理空暂存目录，保持幂等 |

最终 [SQLite 环境原生检查](../../tmp/live-cli-startup-svsb_lt7/native-initializer-matrix.json)、[PG/S3 环境原生检查](../../tmp/live-cli-startup-0ruu6utz/native-initializer-matrix.json) 各覆盖 7 个恢复窗口，并验证 7 个明确文件系统故障：不匹配残片、不同冻结输入、符号链接、硬链接、无绑定旧残缺 home，以及发布瞬间出现同名 owner 文件或空 home 目录。目标 inode/字节和原暂存文件均保留；完整旧格式 owner 的原字节也保留。

每套另在独立挂载/PID 命名空间创建 **64 KiB tmpfs**，由真实 nobody 子进程写入测试数据。初始化写到 **4,096/16,384 字节 config** 时收到内核 **ENOSPC（errno 28）**；仅释放该测试自己创建的 filler 后，生产初始化器成功续写并完成初始化。父挂载命名空间未改变，子进程退出后实际 umount 并删除空挂载点。见 [SQLite 环境空间证据](../../tmp/live-cli-startup-svsb_lt7/native-initializer-space.json)、[PG/S3 环境空间证据](../../tmp/live-cli-startup-0ruu6utz/native-initializer-space.json)。没有填满宿主磁盘。

原生故障测试完成后，每套平台均使用包含这些保留测试目录的真实 native 库存，为新的隔离用户实际启动 Linux CLI、完成 initialize 并核验正常停止；用户/节点初始化信用按原有 133 字节 prepared 路径结清。平台数据库分别为 SQLite 和临时 PostgreSQL，后者还使用真实 S3。

最终计 **20 项实际基础设施场景**（14 个恢复窗口、2 次真实 ENOSPC、2 个旧完整格式兼容、2 个平台 CLI 启闭），另 **14 个明确文件系统故障条件**。14 个恢复窗口各启动两个实际恢复进程，共 28 个并发重试进程；加负例准备时的真实中断，最终共 22 次 SIGKILL。原生中断发生在直接运行初始化器的受控子进程中，不冒充在平台 API 内逐个边界中断；平台路径本轮验证的是正常初始化、计量、prepared 结清和停止。整段 API 崩溃/完整 CLI 范围恢复仍单独跟踪。

## 真实模型、清理和后续

两套最终验收的真实平台 Agent 分别调用 read_text_file 读取本套证据摘要，再验证流式与持久答案一致。最终 **4 次 api.deepseek.com / deepseek-flash，全部 HTTP 200，17,114 tokens**；前序验收另 2 次、8,558 tokens，本阶段总计 **6 次、25,672 tokens，MOCK 0 次**。[SQLite 模型回执](../../tmp/live-cli-startup-svsb_lt7/live-result.json)、[PG/S3 模型回执](../../tmp/live-cli-startup-0ruu6utz/live-result.json)。CLI 内部模型回合仍未验收。

测试 API、原生子进程、对象存储均已退出，private tmpfs 挂载已卸载，临时 PostgreSQL 已删除，开发数据库未写入。测试目录与未知内容保留供审计。原生辅助脚本和实际执行 fixture 均保留于证据和源码快照。

后续继续平台 API 中断与原生部分写入的组合、完整 CLI 进程范围/会话/执行锁恢复、旧根和旧实现的历史维护、审计文件安全维护、OS 硬额度和全局预算，以及真实 CLI 模型/审批/turn/fork。真实 tmpfs ENOSPC 证明初始化器的恢复行为，不代表整个应用已拥有原生磁盘硬配额。

主线 **33/53 完成，剩 20（3 进行中、17 待办）**；资源 **11/17、剩 6**；全功能 **295 项**目标保持，N4-05 仍进行中。
