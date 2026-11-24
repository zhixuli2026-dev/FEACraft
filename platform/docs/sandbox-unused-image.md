# 沙箱启动取消后的镜像回收

WSL 终端启动时，宿主进程会先创建 128 MiB 私有镜像，再向 PTY 门发送 shell 放行消息。上轮实际断线测试中，放行前取消已经停止进程并清空 S3 工作副本和节点预留，镜像仍占用节点磁盘。本轮让同一次启动的所有者在退出证明齐全后回收该镜像。[原始复现](../../tmp/live-cli-startup-qwu1p75n/terminal-preparation.json) · [启动流程](../server/app/services/terminal_sessions.py)

发送放行消息前，终端记录是否尝试过放行。在已尝试发送但应答丢失的情况下，仍按可能已经运行处理，保留镜像。确认从未发送、原生进程和 PTY 门均已退出时，由所属 WSL 的受控维护进程处理私有目录。该进程检查路径、镜像身份、预期文件、Linux 文件锁和 loop 设备挂载，只处理本次创建的固定镜像文件。目录发现其他证据、磁盘仍被占用或无法核验时，保留内容并记录 `SandboxStartupImageCleanupIncomplete`。正常退出继续执行已有导出并保留镜像和归档。[放行边界](../server/app/services/owned_terminal.py) · [回收入口](../server/app/services/sandbox_export_execution.py) · [Linux 回收程序](../server/app/services/sandbox_unused_image.py)

此路径适用于仍有原启动所有者和运行时证据的取消。历史镜像缺少放行证明、API 被强制停止和无法核验的状态，继续进入原有维护与恢复工作。普通 WSL 终端仍使用 `nobody`。

## 设备与权限测试

独立 WSL 进程实际持有 Linux 文件锁时，32 字节的部分准备镜像保持原样。持锁者退出后，同一镜像和其临时目录被回收。以 `nobody` 调用维护程序被拒绝，物理内容原样保留，由所属 WSL 的维护身份运行时可清理。[文件锁实测](../../tmp/sandbox-unused-image-20261119/native-image-lock.json) · [账户权限实测](../../tmp/sandbox-unused-image-20261119/native-image-owner.json)

## 2026-11-19 验收

四组在真实沙箱门登记、尚未放行 shell 时断开 WebSocket。每组都观察到实际停止回执，取消镜像数为零。故障对照在待回收目录加入一份未知文件，实际停止后镜像和文件均仍在，历史状态记录回收未完成。随后的正常沙箱执行命令、退出、导出并从算例读回；正常退出的镜像仍然保留。普通 Windows cmd 和非特权 WSL 的写入、重连和停止也通过。[验收脚本](../server/live_llm_terminal_preparation_acceptance.py)

| 部署 | 基础设施及故障证据 | 真实模型回读 |
| --- | --- | --- |
| SQLite 与文件系统 | [回收、保留和实际导出](../../tmp/live-cli-startup-jifo49wf/terminal-preparation.json) | [2 次 DeepSeek HTTP 200](../../tmp/live-cli-startup-jifo49wf/live-result.json) |
| PostgreSQL 与文件系统 | [回收、保留和实际导出](../../tmp/live-cli-startup-qoepzzyw/terminal-preparation.json) | [2 次 DeepSeek HTTP 200](../../tmp/live-cli-startup-qoepzzyw/live-result.json) |
| SQLite 与 S3 | [回收、保留和实际导出](../../tmp/live-cli-startup-g6gs1m46/terminal-preparation.json) | [2 次 DeepSeek HTTP 200](../../tmp/live-cli-startup-g6gs1m46/live-result.json) |
| PostgreSQL 与 S3 | [回收、保留和实际导出](../../tmp/live-cli-startup-9_uluf_w/terminal-preparation.json) | [2 次 DeepSeek HTTP 200](../../tmp/live-cli-startup-9_uluf_w/live-result.json) |

最终四组共 8 次真实 DeepSeek HTTP 200，模拟调用 0 次。依赖默认 WSL 用户的最初四组另有 8 次真实回读。最终版本显式使用所属 WSL 的维护身份，普通终端的权限保持非特权。锁与权限测试属于实际设备和故障检查。[执行日志](../../tmp/sandbox-unused-image-20261119)
