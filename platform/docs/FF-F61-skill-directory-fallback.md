# FF-F61 技能创建、覆盖和目录回退

2026-11-21，个人和组织技能的普通编辑、ZIP 安装共用版本发布记录。目录切换前记录两版目录标识，读取、打包与写入在同一目录锁内核对现场。新版发布前的中断恢复旧目录，发布后的中断保留新版。兼容旧版留下的唯一有效备份。多份旧备份进入待确认状态，保留现场。

SQLite 与 PostgreSQL 各通过三个实际 API 退出窗口。个人 ZIP 安装发布前退出恢复旧正文和附件，组织 ZIP 安装发布后退出通过打包读回新正文和附件，个人编辑发布前退出恢复旧版本。重启后默认覆盖请求返回 409，显式覆盖可重试完成，暂存及发布记录均收尾。双库各 11 项权限、API 和取消专项也通过，实际文件与 ZIP 测试通过 32 项。[SQLite 恢复](../../tmp/skill-archive-api-hy2il2d8/crash-recovery.json) · [PostgreSQL 恢复](../../tmp/skill-archive-api-hibqz2lk/crash-recovery.json) · [临时数据库](../../tmp/skill-archive-postgres-upgdguyr/result.json) · [实物 ZIP](../../tmp/skill-archive-service-xqud1aia/result.json)

非模拟的 DeepSeek 验收复现了发布前 API 退出。重启后由真实 Agent 回合发现并读取恢复后的私有技能，正文中的唯一验证串原样返回，两次上游 HTTP 请求成功。[恢复后的真实模型证据](../../tmp/live-skill-install-recovery-glonigi1/live-result.json) · [实际工具结果](../../tmp/live-skill-install-recovery-glonigi1/skill-events.json)

编辑所需的本地空间准入包含保留附件的暂存副本和发布记录，224 KiB 的实际附件在复制时已被预留，提交结束后预留归零。目录锁文件为零字节，Windows CRT 支持该字节范围锁法。[本地空间验收](../../tmp/skill-storage-service-ae8vvupl/result.json) · [Microsoft CRT 锁文档](https://learn.microsoft.com/en-us/cpp/c-runtime-library/reference/locking?view=msvc-170)

认证 SeaweedFS 与 PostgreSQL 配合两个隔离 API 完成容量复验，核对同账户竞争、个人额度为零时的拒绝、个人与组织用量归属、满节点拒绝和全部暂存收尾。临时数据库已删除，对象服务已停止。[双 API 容量验收](../../tmp/skill-storage-api-m7fbcf2r/capacity-result.json) · [数据库收尾](../../tmp/skill-storage-postgres-pwud8w22/result.json) · [对象服务收尾](../../tmp/s3-attempt-store-3d2vk8zn/skill-storage-result.json)

实现与恢复入口 [skill_installation.py](../server/app/services/skill_installation.py) · [skill_store.py](../server/app/services/skill_store.py) · [技能档案回归](../server/skill_archive_acceptance.py)

## 先前验收与故障记录

2026-11-21，修复个人 `SKILL.md` 损坏时列表与详情回退结果不同的问题。缺失头部、无效 UTF-8 和文件删除现在都能继续读取组织或内置定义。SQLite 和 PostgreSQL 实际验证两个组织内的技能同时可见、有效个人版本优先、损坏或移除个人文件后恢复共享组织版本，以及普通成员不能向组织安装或导出。Edge 回归另核对了内置技能和个人元数据。测试未发送模型请求。[SQLite API](../../tmp/skill-archive-api-bo0rhjf0/result-acceptance.json) · [PostgreSQL API](../../tmp/skill-archive-api-42wq4bj9/result-acceptance.json) · [Edge](../../tmp/skill-metadata-6njkztkc/skill-metadata-result.json)

真实文件和 ZIP 复跑通过 32 项。包括用户和组织打包安装、覆盖时保留附件、100 个文件和 1 MiB 边界、损坏归档拒绝、以及覆盖安装的第二次目录重命名失败后恢复原目录。两库隔离 API 各 11 项通过，覆盖权限、默认拒绝覆盖、实际解压中取消、归档可用性。临时 PostgreSQL 库已删除。[文件和 ZIP](../../tmp/skill-archive-service-8x34gmjc/result.json) · [数据库收尾](../../tmp/skill-archive-postgres-mtpq92dr/result.json)

修复前，SQLite 隔离 API 在旧目录改名为备份后实际退出，原技能正文可以从备份读回。重启后列表仍显示该技能，详情返回 404。[原故障证据](../../tmp/skill-crash-diagnostic-20261121-135143/evidence.json)
