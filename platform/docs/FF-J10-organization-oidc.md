# FF-J10 组织 OIDC 登录

一个工作台可配置多个组织身份源。浏览器通过授权码和 PKCE S256 登录。数据库保存短时事务、浏览器绑定与加密的 PKCE verifier，任一 API 实例都可处理同一事务的回调。一笔授权只能处理一次。服务端从已配置的身份源发现签名公钥，核验 ID Token 的签名、签发方、受众、有效期和 nonce。需要补取邮箱时，还会核对 UserInfo 的 subject。 [协议实现](../server/app/services/oidc.py) · [事务实现](../server/app/services/website_auth.py) · [OpenID Connect Core](https://www.openid.net/specs/openid-connect-core-1_0.html)

已有平台账户的同名邮箱需要通过原有方式登录，再在「设置 → 账户与登录」明确关联。组织身份按发行方、客户端和 subject 匹配。平台使用 HttpOnly 会话 cookie，服务端退出和解除关联可跨 API 实例撤销会话。账户只有一个可用登录方式时保留该关联。通过配置开启身份源退出时，工作台先撤销本地会话，再使用该身份源公布的注销地址。 [RP 发起退出规范](https://openid.net/specs/openid-connect-rpinitiated-1_0.html)

## 配置

设置 `VIBEFLOW_OIDC_PUBLIC_ORIGIN` 为工作台的固定 HTTPS 源，并将 `VIBEFLOW_OIDC_PROVIDER_FILE` 指向服务可读的私有 JSON 文件。所有 API 实例使用同一数据库、签名密钥、站点源与身份源配置。生产 Compose 可使用 `docker-compose.oidc.yml` 叠加层。设置中的身份源标识用于形成精确的回调地址 `<站点源>/api/v1/auth/oidc/<身份源标识>/callback`。在身份平台为每个客户端登记对应回调。 [生产叠加配置](../infra/docker-compose.oidc.yml)

```json
{
  "engineering": {
    "name": "工程身份",
    "issuer": "https://identity.example.org/realms/engineering",
    "client_id": "vibeflow-workbench",
    "token_auth_method": "client_secret_basic",
    "client_secret": "<由部署密钥文件提供>",
    "rp_logout": true
  }
}
```

公共客户端选 `none` 并省略 client secret，传统客户端可选 `client_secret_post`。发现的授权、令牌、公钥和用户信息地址默认只能使用 issuer 的源。需要访问身份源指定的另一可信 HTTPS 源时，在对应条目中显式设置 `endpoint_origins`。启用 `rp_logout` 的客户端需登记工作台根地址为退出回跳。反向代理的事件记录只保留回调路径，授权码和 state 不写入日志。

单一身份源的旧变量仍可加载为 `default`。已登记的同源 `/oidc/callback` 入口由网页转交服务端校验。会话退出沿用统一的 `/api/v1/auth/logout`。ChatGPT 网站登录沿用其已有应用配置和独立入口。

## 实测

- 官方 Keycloak 26.8.2 的两个真实 realm 分别提供 confidential 与 public client。SQLite 和 PostgreSQL 各运行两个 API 实例。由主 API 发起，第二个 API 接收实际回调，并处理申请浏览器不匹配、取消和重放。真实 Keycloak 签发但 nonce 错误的 ID Token 被拒绝。同邮箱碰撞要求先以原方式登录并显式关联。 [SQLite](../../tmp/ff-j10-oidc-primary-0s1tlscf/oidc-result.json) · [PostgreSQL](../../tmp/ff-j10-oidc-primary-v9bh16ak/oidc-result.json) · [官方 Keycloak](https://github.com/keycloak/keycloak/releases/tag/26.8.2)
- 实际 Edge 完成显式关联、另一个身份源独立创建账户、平台及 Keycloak 登出、解除关联与最后一种登录方式保护。退出前的 cookie 在第二个 API 返回 401。浏览器无法读取 HttpOnly cookie，JavaScript 存储中没有会话 bearer。390px 账户区宽度与滚动宽度均为 366px。 [SQLite 浏览器](../../tmp/ff-j10-oidc-primary-0s1tlscf/oidc-browser.json) · [PostgreSQL 浏览器](../../tmp/ff-j10-oidc-primary-v9bh16ak/oidc-browser.json) · [桌面](../../tmp/ff-j10-oidc-primary-v9bh16ak/oidc-login-1440.png) · [移动](../../tmp/ff-j10-oidc-primary-v9bh16ak/oidc-account-390.png)
- PostgreSQL 最终验收中第二个 API 记录了七条回调路径，均无 code 或 state 参数。隔离 Keycloak 进程和 PostgreSQL 临时库清理完成。前端构建、Compose 配置检查均通过。两轮模型调用 0 次。正式环境使用隔离验收中相同的服务契约，部署时需在目标身份源注册本站回调。 [回调日志](../../tmp/ff-j10-oidc-peer-k16l98oz/api.log) · [进程清理](../../tmp/ff-j10-oidc-primary-v9bh16ak/keycloak-process.json) · [数据库清理](../../tmp/terminal-agent-postgres-eeqkcvro/cleanup.json)
