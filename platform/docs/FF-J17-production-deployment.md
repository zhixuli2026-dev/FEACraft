# FF-J17 单机生产 Compose 与 Linux 执行器

同一台 Docker 主机已运行 PostgreSQL、Redis、SeaweedFS、API、独立 worker 和 Nginx Web。历史隔离验收覆盖实际迁移、注册、算例、大文件读写、Redis 作业执行与取消、ZIP 恢复，以及真实 DeepSeek 读取隔离文件。生产配方支持默认 FS 及显式 Redis/SeaweedFS 配置。 [部署手册](deployment.md) · [实际 Compose 业务](docker-deployment-acceptance.md) · [工具链矩阵](deployment-environment-matrix.md)

- 2026-11-24 在修复后的实际 Linux API 镜像中验证本地 PTY。线协议继续使用现有 `shell=cmd`，Linux 宿主选择固定 Bash，Windows 宿主选择现有 CMD。Linux PTY 维度为 29×91，发送命令、断开重连并读回 backlog、显式关闭和实际范围清空均通过。外账号查询返回 404。 [Linux 镜像运行回执](../../tmp/FF-J17-production-pty-20261124/linux-release-image-pty.log) · [PTY 进程范围](../server/app/services/owned_terminal.py)
- 初次候选启动曾暴露 Windows 专用 Elmer 路径在 Linux 导入出错。另一次启动发现 Linux PTY 仍使用 Windows 启动路径。修复后重新构建本机候选，不带 Windows 服务端源码挂载运行通过。实际 Windows 与 WSL PTY 的命令及收尾也复验通过。 [候选构建](../../tmp/FF-J23-release-lock-20261124/api-release-runtime-build.log) · [双宿主回执](../../tmp/FF-J17-production-pty-20261124/windows-wsl-regression.json) · [失败证据](../../tmp/FF-J17-production-pty-20261124/linux-pty-diagnostic.log)
- PostgreSQL/FS 与 PostgreSQL/SeaweedFS 实际升级、降级、回滚副本、联合快照和原 provider 密钥恢复已有签收。独立恢复环境用备份前的加密配置完成真实 DeepSeek 文件读取。 [升级和回滚](FF-J12-completion-review.md) · [联合恢复手册](deployment-recovery-runbook.md) · [恢复后的真实模型](../../tmp/live-deployment-restore-1f693cfb2ec0/result.json)

本次新增 PTY 检查属于实际基础设施，模型调用 0。目标是用户确认的同一台 Docker 主机。跨物理宿主、对外 TLS、KMS、各专业许可和最终 SBOM 仍按对应任务跟踪。 [本机部署回执](N7-05-deployment-acceptance.md) · [当前发布锁](FF-J23-release-lock.md)
