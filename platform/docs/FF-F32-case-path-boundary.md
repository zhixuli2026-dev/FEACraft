# FF-F32 工作区、最近父目录与符号链接防护

已登记的本地存储前缀必须解析到原目录。算例根和账户存储目录一旦被链接重定向，读取、文件树和写入均拒绝。算例内普通路径仍按最近存在的父目录解析，目标即使尚未创建也须留在同一算例内。算例内指向普通文件的链接可以读取。 [存储路径](../server/app/storage.py)

隔离 API 实测普通文本与字节范围读取、跨算例及相似宿主目录、悬空链接、尝试创建缺失目标、算例根和账户目录重定向。撤销链接后复验两案的原文件。另一账户直接读取返回 404。实际 DeepSeek 的 Plan 文件工具读取内链并拒绝跨算例链接，拒绝事件和回答均无对端内容。Windows 和非特权 WSL 原生 Codex 正常读取专项通过。 [HTTP 与 DeepSeek](../../tmp/live-f32-case-path-19bbmkze/live-result.json) · [Windows 原生](../../tmp/live-codex-permissions-2fww65ug/live-result.json) · [WSL 原生](../../tmp/live-codex-permissions-0cnopzpl/live-result.json)

本阶段有 4 次真实 DeepSeek HTTP 200，另有两个原生回合上报用量，模拟 0。7 个实际文件系统测试逐项通过。两次早期夹具失败发生在模型调用前，分别修正邮箱保留域名和账户存储目录层级判断。 [阶段汇总](../../tmp/FF-F32-case-path-20261121/validation.json) · [文件系统测试](../../tmp/f32-symlink-smoke-20261121.log)
