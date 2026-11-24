# 托管网站 Sign in with ChatGPT

2026-11-03 最新用户决定：正式 client 注册、真实域名／回调配置和 OpenAI 联调暂缓（“没有，这部分暂时不用管”）。保留本页已完成的代码与隔离证据，缺少配置时功能仍关闭；不继续索取注册信息，不将真实联调计为通过。后续部署步骤仅在用户明确恢复此项后执行，其余主线继续。

2026-11-03，N4-03.g2b3b。按用户已确认的“对外托管或商业服务，采用 API / Sign in with ChatGPT”实现网站身份登录。模型请求继续使用 [ProviderProfile API 凭据](hosted-cli-credentials.md)，不会用网站 ID token 调用模型，也不会读取服务器个人 CLI 登录。

本单元代码、迁移与隔离验收已具备；正式 OpenAI client 注册和公网 HTTPS 回调联调仍未完成。全部 53 项主线与 295 项功能继续保留，本单元不将完整账户或 CLI 功能标为已交付。

## 用户流程

登录页在管理员配置完成时显示“使用 ChatGPT 登录”。登录成功后回到工作台，旧浏览器 JWT 被清除，第一方 HttpOnly Cookie 用于 API 和 WebSocket。失败提示会说明重新登录、联系管理员或先登录原账户再关联的恢复方法；页面不保留 OAuth code/state。

设置中的“账户与登录”显示当前平台账户及已关联 ChatGPT 账户，支持再次关联、取消尚未完成的授权、解除关联和退出。解除关联会撤销该身份对应的所有网站会话；唯一登录方式不能被删除。退出先在服务端撤销当前网站会话，再清除 Cookie 和浏览器旧令牌并重新加载页面。服务端操作失败时显示错误，不假装已经退出。

身份以 `(issuer, client_id, subject)` 唯一绑定，不按邮箱自动合并。邮箱碰撞要求先用原有方式登录，再显式关联；已关联他人的身份不能被接管。缺少邮箱的新身份需先完成平台注册，再关联。沿用平台现有注册策略：邮箱只是联系字段，`email_verified=false` 单独保留，不能作为账户所有权或找回凭据。已停用平台账户不能创建或继续使用网站会话。

## 授权与持久状态

协议实现：[siwc_protocol.py](../server/app/services/siwc_protocol.py)。固定 OpenAI issuer，发现文档的授权、令牌与 JWKS 端点必须同源；后端网络请求不跟随重定向、不读取环境代理，响应大小与超时有上限。授权使用 `openid profile email`、S256 PKCE、独立 state/nonce 和浏览器绑定值。公开 client 使用 `none`，机密 client 使用注册的 `client_secret_basic`，不自动回退认证方法。

用 PyJWT 验证 RS256/ES256 签名、算法与 kid、issuer/audience、时效、nonce、多 audience 的 azp；未知 kid 刷新 JWKS。令牌响应只要求 ID token，不要求 access/refresh token；不保存或向 JavaScript 返回任何提供方令牌。契约依据为 [OpenAI 网站登录文档](https://developers.openai.com/siwc/website)。

[website_auth.py](../server/app/services/website_auth.py) 管理三张表：

| 表 | 内容与保留边界 |
|---|---|
| `website_oauth_transactions` | state/browser/nonce/config 摘要、登录或关联意图、10 分钟有效期；PKCE verifier 用应用密钥派生的独立 Fernet 密钥加密，终态清除密文 |
| `external_identities` | 身份三元组、平台用户、提供方邮箱与验证标记、创建/解除时间；解除后保留 tombstone |
| `website_auth_sessions` | 不透明会话 token 的摘要、身份/用户、可选 provider sid、8 小时有效期与撤销时间 |

事务状态为 `pending → processing → completed/failed/cancelled/expired`。交换授权码之前先持久认领；两个 API 竞争同一回调只允许一个交换。取消、替换、过期与迟到回调不能复活事务。身份解析、新账户/默认工作区、会话创建和授权完成同事务提交。用户与身份按固定顺序加锁，避免登录与解除关联交叉等待。

同一配置与应用密钥下，待完成授权和已有会话能跨 API 实例或重启继续。已进入 `processing` 而失去执行者的交换不会自动重放；至迟过期清理后须重新登录。过期扫描每分钟运行，也在启动授权、回调和状态查询时执行。配置变更使旧授权失效。

Cookie 为 `__Host-vf_chatgpt_state` 和 `__Host-vf_session`，均使用 Secure、HttpOnly、SameSite=Lax、Path=/；不设置 Domain。所有使用 Cookie 的写请求及 WebSocket 建连验证精确 Origin。显式无效 Bearer 不回退为 Cookie。普通 REST、事件/CAD/终端 WebSocket、可视化网关已接通公共鉴权入口；可视化转发剥离 Cookie/Authorization/Referer，并拒绝上游 Set-Cookie；旧 per-view JWT Cookie 不覆盖当前网站会话。

浏览器中聊天流、工具批准、导入导出、作业日志和图片读取等直接 fetch 入口，不再在无 JWT 时发送 `Bearer null`。

## API

以下路径前缀均为 `/api/v1/auth`，授权响应不缓存：

| 方法与路径 | 作用 |
|---|---|
| GET `/chatgpt/status` | 配置可用性和本浏览器授权事务的公开状态 |
| POST `/chatgpt/start` | `{intent:"login"\|"link"}`；link 必须已登录；返回授权地址并设置浏览器绑定 Cookie |
| POST `/chatgpt/cancel` | 取消本浏览器尚未结束的授权，清除临时 Cookie |
| GET `/chatgpt/callback` | 服务端换码和校验；303 返回本站 `/?chatgpt=<安全结果码>` |
| GET `/chatgpt/connections` | 当前用户关联列表，不返回 subject/token |
| DELETE `/chatgpt/connections/{id}` | 解除所属关联并撤销对应网站会话，保护最后一种登录方式 |
| POST `/logout` | 撤销当前网站会话并取消当前待完成授权，清除 Cookie |

旧密码登录、旧 JWT 与通用 OIDC 接口继续兼容。旧 JWT 是无服务端撤销记录的令牌，退出界面只清除浏览器副本；没有将其包装成全局撤销能力。

## 部署准备与迁移

配置模板见 [server/.env.example](../server/.env.example)。在正式注册完成前留空 `VIBEFLOW_SIWC_CLIENT_ID`，功能默认关闭。所需值：

- `VIBEFLOW_SIWC_CLIENT_ID`：正式注册的 client ID。
- `VIBEFLOW_SIWC_PUBLIC_ORIGIN`：用户实际访问的 HTTPS origin，不含路径，例如占位地址 `https://vibeflow.example.com`。
- 回调必须精确注册为 `<public_origin>/api/v1/auth/chatgpt/callback`。
- `VIBEFLOW_SIWC_TOKEN_AUTH_METHOD`：注册为 `none` 或 `client_secret_basic`；后者通过部署密钥配置提供 `VIBEFLOW_SIWC_CLIENT_SECRET`，不要放入前端或文档。
- 所有 API 实例共享数据库及至少 32 字符的强随机 `VIBEFLOW_JWT_SECRET`；轮换密钥会使待完成 PKCE 失效。前端与 API 使用同一公开 origin，CORS 配置对应此 origin。

后端会从 Uvicorn 回调访问日志中移除 query。反向代理、负载均衡器、APM 和外部访问日志也须对回调省略 query，并避免记录 Cookie/Authorization；不能依赖应用修改已经被上游记录的 URL。部署时核对 TLS、代理信任范围和 exact callback，不能以任意 Host 头生成回调。

新增 Alembic `0a8f65b9e421`，前置 `f97e54a8d310`，当前共 33 张业务表。迁移新增三表；降级时只要任一身份/授权/会话表有记录就拒绝删除，需先制定数据保留与回退方案。当前开发库仍是旧版本，不能直接重载新 API；必须先备份并审阅实际库迁移。本单元只迁移临时 SQLite/PostgreSQL。

## 验收与后续

可重复后端套件：[website_auth_smoke.py](../server/website_auth_smoke.py)，运行 `python -X utf8 -B website_auth_smoke.py --postgres`。本地签名 OIDC 服务与真实 HTTPS 产品 API 完成授权往返，浏览器/HTTP 客户端实际使用 Secure Cookie；包括签名错误矩阵、密钥轮换、显式关联、取消竞争、API 重启、多实例认领/撤销、事件 WebSocket Origin、可视化凭据隔离和解除关联时迟到登录。测试没有生产协议绕过开关，local issuer 仅在独立测试 app 中注入。

汇总证据位于 `tmp/mainline-N4-website-auth-evidence/`：后端、双库迁移、浏览器 14 项、前端 build、核心回归、只读开发库快照和归档逐文件校验。浏览器覆盖手机宽度、200% 缩放、焦点、加载/失败恢复、取消、旧 JWT 清除、唯一登录方式保护、退出、密码注册与关联失败保留原账户。真实 OpenAI client 与远端推理均未使用。

仍待：正式注册/公网回调联调；提供方安全通知与 sid 撤销、跨设备会话列表和主动断开既有长连接；旧通用 OIDC 的等价持久化、旧 JWT 全局撤销、生产限流与审计/保留策略。当前撤销在后续请求和新 WebSocket 握手生效，已受理工作不因退出而自动取消。原生 CLI sandbox 设置、WSL 后端与完整聊天 UI 继续 N4-03/N5。
