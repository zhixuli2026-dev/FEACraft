# FF-J09 组织模型配置与远程密钥管理

模型配置可归个人或组织持有。组织成员可使用组织配置，管理权随组织管理员角色变更。浏览器及 API 只返回配置元数据和密钥存在状态。组织配置可用于平台 API 模型调用与 API Agent，交互式终端及原生 CLI 使用个人凭据。个人 Fernet 配置和先前共享仍可按原有方式使用。 [模型配置 API](../server/app/routers/llm.py)

组织模型凭据用每条配置独立的 AES-256-GCM 数据密钥加密。OpenBao Transit 封装数据密钥，绑定组织编号、配置编号和服务地址。数据库存储凭据密文及封装结果。组织允许变更供应商或地址时要一并提交新 API 密钥。OpenBao 不可用时调用失败，凭据不会落入个人 vault 或进入模型请求。 [加密实现](../server/app/services/provider_kms.py) · [OpenBao Transit API](https://openbao.org/docs/next/api/secret/transit/)

## 启用

执行数据库迁移 `e87ba491d832`。在独立 OpenBao 实例启用 Transit，为每个使用组织模型配置的组织建立一把派生密钥。以下是部署标识 `vf-production` 和组织编号 `42` 的示例。

```bash
bao secrets enable transit
bao write transit/keys/vf-production-org-42 type=aes256-gcm96 derived=true
```

应用的短期或可续期 token 只需以下操作。启用 Transit、创建密钥、外层版本轮换和恢复由 OpenBao 操作员执行。将应用 token 存入服务读取的私有文件。服务器每次操作读取当前文件，轮换服务 token 时原子更新该文件。 [OpenBao 策略](https://openbao.org/docs/next/concepts/policies/)

```hcl
path "transit/encrypt/vf-production-org-*" { capabilities = ["update"] }
path "transit/decrypt/vf-production-org-*" { capabilities = ["update"] }
path "transit/rewrap/vf-production-org-*"  { capabilities = ["update"] }
```

配置 `VIBEFLOW_PROVIDER_KMS_ADDRESS` 为可信 HTTPS 源、`VIBEFLOW_PROVIDER_KMS_KEY_PREFIX` 为部署唯一前缀、`VIBEFLOW_PROVIDER_KMS_TOKEN_FILE` 为 token 文件。默认挂载点是 `transit`。自建 CA 可通过 `VIBEFLOW_PROVIDER_KMS_CA_FILE` 指定。标准生产 Compose 附带可选的 `docker-compose.provider-kms.yml` 配置，使用 `platform/infra/.env.example` 中对应的变量。启用后从工作台「设置 → 管理模型配置」新建配置并选组织归属。 [生产叠加配置](../infra/docker-compose.provider-kms.yml) · [迁移](../server/migrations/versions/e87ba491d832_organization_provider_profiles.py)

## 轮换和恢复

供应商 API 密钥由组织管理员在配置表单更新。外层加密密钥由 OpenBao 操作员执行 `bao write -f transit/keys/vf-production-org-42/rotate`。之后由组织管理员逐个选择配置并执行「更新密钥加密版本」，或调用 `POST /api/v1/provider-profiles/{id}:rewrap`。该操作升级已保存的数据密钥封装，同版本重复执行会直接返回当前状态。替换供应商密钥和更新封装均写入组织审计。

备份配对保存数据库与 OpenBao 的加密存储快照。保留对应的解封材料，使用恢复实例的受限应用 token，沿用原部署前缀和挂载点。用还原数据核对可解密版本，并通过实际模型调用验证恢复。保留恢复点仍需读取的旧密钥版本，之后再调整最小解密版本。个人配置的恢复继续使用其原 Fernet vault。 [OpenBao Raft 存储](https://openbao.org/docs/configuration/storage/raft/)

## 实测

- 使用校验过摘要的官方 OpenBao 2.7.3。以非特权容器运行单节点 Raft，SQLite 走回环 HTTP，PostgreSQL 走验证证书的 HTTPS。受限 token 无权轮换外层密钥，操作员实际轮换到第二版后，应用只更新封装，业务密文保持原样。换入其他配置的封装、改写数据库模型服务地址、KMS 停机均使请求在上游调用前失败。另验证成员无法改配置，原创建者角色禁用后继任管理员可改配置。 [SQLite](../../tmp/live-j09-org-kms-9elsput2/organization-kms.json) · [PostgreSQL](../../tmp/live-j09-org-kms-xd6tngyg/organization-kms.json) · [官方发行包](https://github.com/openbao/openbao/releases/tag/v2.7.3)
- 通过 SQLite 备份、PostgreSQL 临时库副本及停机后的 Raft 存储副本恢复新 API 与 KMS。恢复严格使用第二版组织密钥，两种数据库都成功调用真实模型。两轮各 4 次 DeepSeek `deepseek-flash` HTTP 200，共 8 次，模拟 0 次。两轮执行了实际翻译、流式 Agent、轮换后调用和恢复后调用。临时容器及数据库副本已清理。 [SQLite 恢复 API](../../tmp/live-j09-org-kms-restored-n68whyq7/result.json) · [PostgreSQL 恢复 API](../../tmp/live-j09-org-kms-restored-w6m81eua/result.json) · [PostgreSQL 清理](../../tmp/terminal-agent-postgres-3kx8o9nh/cleanup.json)
- 两种数据库均从前一版本升级并保留旧个人配置密文。前端构建及 Compose 合并检查通过。Edge 在 1440px 验证组织配置创建、密钥掩码及保存后清空，390px 验证成员不可编辑及无横向溢出。迁移、构建和浏览器验收本身调用模型 0 次。现行部署继续使用既有配置。 [迁移证据](../../tmp/ff-j09-kms-migrations-nerujgq6/migration-result.json) · [Edge](../../tmp/live-j09-org-kms-9elsput2/kms-browser.json)
