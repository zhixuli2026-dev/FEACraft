# FF-J25 算例契约与 CLI 配置恢复

设置页“CLI 契约与备份”可为 Codex、OpenCode 和 Kimi 保存算例约定及本人的默认 API 配置。备份包含说明文件原字节、配置编号和所属账户。恢复先备份当前内容，提交复核文件版本。配置密钥仍由原模型凭据库管理。历史的无配置引用快照可继续读取和恢复。

Windows 终端快捷入口从当前算例带入相应工具默认配置，仍可在启动前选择其他配置。工作台 OpenCode 和 Kimi 启动时加入与工具匹配的算例约定。原生线程保存当时的说明快照。续聊沿用原线程，读取更新约定时开始新线程。

- Windows API 使用 PostgreSQL。拒绝其他账户配置，实际切换 OpenCode → Kimi → OpenCode，旧版本恢复返回 409，恢复后的原生线程与新线程都返回只存在于算例约定的随机值。还原初始快照逐字节保留 CRLF。真实 DeepSeek 6 次 HTTP 200。 [Windows 验收](../../tmp/live-acp-contract-fatfcida/acp-contract.json)
- Linux 同宿主 Docker 使用已有固定镜像和独立原生临时文件系统。同样的恢复检查通过，5 次真实 HTTP 200，另有 2 次连接失败后重试，临时容器已移除。首轮 Windows 挂载目录的原子移动返回 EINVAL。改用 Linux 临时文件系统并满足原有 256 MiB 预留后复验。 [Linux 验收](../../tmp/live-acp-linux-contracts-v2j5ancq/live-acp-contract-h3pa3282/acp-contract.json) · [容器清理](../../tmp/live-acp-linux-contracts-v2j5ancq/container-result.json)
- Edge 1440 × 950 和 390 × 844 切换 OpenCode 与 Kimi 并刷新续聊。设置页保存、切换和恢复配置，然后以算例默认配置启动真实 Codex。Codex 读取只在算例文件里的随机值，实际终端关闭后核验了配置编号和退出。6 次 Chat Completions、2 次 Responses HTTP 200。 [浏览器检查](../../tmp/live-acp-edge-b0ebn95e/browser.json) · [真实请求](../../tmp/live-acp-edge-b0ebn95e/acp-edge.json) · [手机配置](../../tmp/live-acp-edge-b0ebn95e/contract-profile-390.png)

三轮共 19 次真实 DeepSeek HTTP 200，模拟 0。迁移 `8da5cff5b71b` 建立原生线程与回合记录，由 `56e2d59f03c1` 升级的独立 SQLite 通过完整性检查。临时验收未升级现有运行实例。第二台物理宿主继续按主线安排。 [迁移结果](../../tmp/ff-j25-acp-migration-rb1tv92_/migration-result.json) · [真实验收入口](../server/live_llm_acp_platform_acceptance.py)
