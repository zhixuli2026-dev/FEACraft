# 部署 provider 加密密钥的持久化与恢复

2026-11-16，N7-05 部署交付子项。旧实现将 provider 凭据加密密钥保存在应用目录，容器重建后可能丢失，导致数据库内已保存的凭据无法解密。

新增 `VIBEFLOW_VAULT_KEY_PATH`，指定部署时已准备好的只读密钥文件。API 启动在数据库初始化前检查文件可读取且为有效 Fernet 密钥；缺失、损坏不生成替代密钥。未配置时兼容已有本机 `app/.vault_key`。没有更换加密格式，没有迁移或覆盖开发密钥。

Compose 的 API/worker 共用 `provider_vault` 文件 secret，默认来源 `infra/secrets/provider-vault.key`。文件留在宿主上，容器重建继续挂载。新增的 `scripts/init-provider-vault.py` 可为全新数据库生成密钥，或用 `--copy-from` 恢复原密钥；目标已存在则拒绝覆盖，输出不含密钥内容。构建上下文排除 secrets 目录、key/pem 文件，发布工具只收录初始化脚本。

已有数据库必须恢复原密钥，不能生成一份格式有效的新密钥代替。具体操作见 [部署说明](deployment.md)。

## 验证范围

显式真实入口 `server/live_llm_vault_persistence_acceptance.py` 读取用户授权的 DeepSeek profile，仅读取开发库。测试在隔离 API/SQLite 中创建加密凭据，通过正式翻译 API 调用真实模型；退出 API，将原密钥复制到新的持久目录，再启动 API、登录并使用同一 profile 再次调用真实模型，不重新提交 API key。

HTTP 观察器只转发并记录实际请求状态、模型和用量，没有模拟回复。缺失/损坏配置及拒绝覆盖另列为纯配置检查，不当作模型验收。两次实际 API 进程均需确认退出。

Compose 渲染检查确认 API/worker 共用挂载和路径。Docker 整栈仍未验证；Windows 上的真实密钥恢复不能冒充 Docker 容器部署通过。N7-05 及主线剩余范围保持。

## 实际结果

`tmp/live-vault-persistence-pl6haqee/vault-result.json` 已通过：正式 DeepSeek `deepseek-flash` 两次 HTTP 200，共 404 tokens；重启后同一个 profile ID 可用，没有重新提交 API key，原密钥字节保持不变，未生成本机回退密钥。模拟模型调用为 0。

两个实际 API 进程均由验收持有者终止并确认退出（exit 1），用于验证进程退出后的凭据恢复；不把这次终止当作优雅关闭验收。缺失/损坏配置和已有目标拒绝覆盖是 3 项配置/文件检查，另列于模型验收之外。

审查登记为 `tmp/N7-05-vault-persistence-review-20261116.json`，核对 631 份入口、辅助及生产源码哈希。Compose 10 组渲染检查通过。没有修改开发数据库或原 `app/.vault_key`，没有对外发布；主线仍 48/53、剩 5 项。
