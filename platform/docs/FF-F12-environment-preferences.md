# 按后端保存模型偏好

Agent 的「保存此会话」在同一个事务中保存会话设置，以及当前 API、Codex Windows 或 Codex WSL 的模型与推理偏好。偏好以用户、会话和后端为单位。旧会话没有单独偏好时使用原会话的默认设置。并发保存不同后端分别更新对应记录。端点、默认模型或已保存凭据变化会使旧偏好待核对，显示名称变化保留偏好。

原生 CLI 的模型目录由真实会话查询，并按账号、算例、会话、模型配置和后端在浏览器进程内缓存三分钟。切换回来时复用目录，修改配置后清除缓存。操作员仍可点击「检查运行环境」取得最新目录。

Edge 六次切换并刷新，三个后端恢复各自的设置。恢复 API 后实际 `deepseek-flash` 请求发送 `reasoning_effort=medium`，上游返回 HTTP 200，回执记录 762 tokens。Windows 和非特权 WSL 的原生 Codex 目录分别返回 11、9 个模型，切回后无需重复查询。实际推理 1 次，模拟 0 次。原生目录检测没有发起推理。首轮 WSL 夹具漏配已分配的非特权私有目录，补齐配置后通过。 [切换和真实模型](../../tmp/ff-f12-environment-preferences-3tp3pkez/preferences-acceptance.json) · [原生目录](../../tmp/ff-f12-native-catalog-zapbt2v9/native-catalog.json) · [事务回执](../../tmp/ff-f12-atomic-edit-w51kq693/atomic-preference.json)

迁移 `d9a7c34018ef` 新增 `conversation_model_preferences` 表。隔离 PostgreSQL 从空库迁移通过并已清理，SQLite 的实际保存、无效参数回滚和浏览器工作流均通过。部署升级时需先运行项目的数据库迁移。 [迁移结果](../../tmp/FF-F12-environment-preferences-20261124/postgres-migration.json)
