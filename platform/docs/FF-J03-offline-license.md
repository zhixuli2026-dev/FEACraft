# FF-J03 平台自有离线许可

内网部署可用 Ed25519 许可启用组织的专业版或团队版配额。许可包签入部署 UUID、组织编号、产品专用设备摘要、套餐、有效期和撤销序号。服务端只持有签发方公钥。签发私钥使用加密 PKCS#8 文件，由签发方保管。现有 B/S 部署可继续按组织订阅运行。历史原版单机授权的设备与激活细节仍缺直接证据。 [签名标准](https://www.rfc-editor.org/rfc/rfc8032.html) · [Ed25519 接口](https://cryptography.io/en/latest/hazmat/primitives/asymmetric/ed25519/) · [系统身份建议](https://www.freedesktop.org/software/systemd/man/latest/machine-id.html)

## 签发与部署

在 `platform/server` 下执行。目标机先运行 `python -m app.services.offline_license_admin device`，将返回的产品专用摘要和目标组织编号交给签发方。Linux 需要持久系统标识，容器部署应由部署方提供持久只读的 `/etc/machine-id`。同一离线部署应在 API 和 worker 配置相同的许可包、公钥、部署 UUID 与撤销序号下限。

```powershell
python -m app.services.offline_license_admin generate-key --private-key issuer-private.pem --public-key issuer.pem
python -m app.services.offline_license_admin issue --private-key issuer-private.pem --deployment <UUID> --org-id <ID> --plan pro --device <摘要> --days 365 --output license-v1.json
python -m app.services.offline_license_admin issue --private-key issuer-private.pem --deployment <UUID> --org-id <ID> --plan pro --device <新设备摘要> --days 365 --previous license-v1.json --output license-v2.json
python -m app.services.offline_license_admin revoke --private-key issuer-private.pem --previous license-v2.json --output license-revoked.json
```

设置 `VIBEFLOW_OFFLINE_LICENSE_BUNDLE_PATH`、`VIBEFLOW_OFFLINE_LICENSE_PUBLIC_KEY_PATH` 和 `VIBEFLOW_OFFLINE_LICENSE_DEPLOYMENT` 启用此模式。换机或续签时使用当前许可作为 `--previous`，新包携带旧许可撤销项。撤销在目标部署收到更新包后生效。发布新包时更新 `VIBEFLOW_OFFLINE_LICENSE_MIN_REVOCATION_SEQUENCE`，可拒绝用旧撤销序号回放之前的包。配置路径由部署方管理，可同目录原子替换许可文件。设置页显示实际许可状态、到期日和生效配额。签名不通过、过期、撤销、目标不匹配时使用免费配额。

## 验收

- Windows PostgreSQL 和 SQLite 分别使用真实加密签发密钥、受信公钥及实际 HTTP。检查许可启用、改动已签条款、另一个签发方、组织不匹配、到期、设备迁移、撤销及重新发放。专业版 10 席、团队版 50 席、免费版 2 席按相同配额入口生效。独立 SQLite 实际重启后，部署的撤销序号下限拒绝旧包。有效许可下共 4 次官方 DeepSeek HTTP 200，免费额度拒绝在模型请求前完成，模拟模型 0 次。 [PostgreSQL](../../tmp/live-j03-offline-license-yrhnu033/license-acceptance.json) · [SQLite 与重启](../../tmp/live-j03-offline-license-xsd8av1m/license-acceptance.json)
- 同一物理宿主的 Linux 容器带持久的隔离系统标识，以 `--network none` 测试实际 API。Windows 的签名包在 Linux 为设备不匹配，从旧包签发到 Linux 后生效，再撤销回免费配额。旧许可序号已进入撤销清单。测试容器已移除，模型 0 次。 [Linux 换机](../../tmp/ff-j03-linux-license-wsu638xa/transfer-result.json)
- Edge 1440px 和 390px 显示真实团队版用量、离线许可生效和到期日。在线支付控件随离线模式隐藏。浏览及布局检查模型 0 次。原运行实例保持现有配置。 [浏览器](../../tmp/live-j03-offline-license-xsd8av1m/license-browser.json) · [手机显示](../../tmp/live-j03-offline-license-xsd8av1m/offline-license-390.png)
