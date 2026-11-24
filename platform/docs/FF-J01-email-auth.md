# FF-J01 邮箱、密码和用户状态

数据库唯一索引防止同一个邮箱注册多个账号。两个请求同时注册同一个邮箱时，先完成的请求创建账户、默认工作区和个人组织。另一请求在检测到唯一性冲突后回滚，并返回 409 邮箱已注册。顺序重复提交也返回 409。密码用既有 PBKDF2 加盐存储。 [认证路由](../server/app/routers/auth.py) · [密码和 JWT](../server/app/security.py)

使用隔离 SQLite 和正式 HTTP 路由，在真实密码哈希之前让两个注册请求到达同一点。旧版返回 200 和 500。修复后返回 200 和 409，读回只有一个账号和一个默认工作区。两个隔离 API 都已退出。 [修复前结果](../../tmp/email-auth-j01-5hewxrlx/email-auth-result.json) · [修复后结果](../../tmp/email-auth-j01-ibrr4f_j/email-auth-result.json)

正确密码可以登录并用 Bearer 获取账户及工作区。无令牌、错误密码、过期令牌及签名不匹配均返回 401。停用账户后，新登录与已有令牌的访问都会返回 401，恢复为 active 后可以登录。无效邮箱返回 422。此次验收使用实际密码和 JWT 校验，模型调用为 0。 [已登记验收入口](../server/auth_email_acceptance.py)
