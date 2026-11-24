# FF-J04 个人组织与默认订阅

2026-11-21，实际 PostgreSQL 中，同一老账户在两个 API 首次打开组织页面时分别得到组织 3 和 4。两个组织各有免费订阅。修复后在用户行的写事务内重新检查已有组织，再统一创建组织、管理员成员和免费订阅。注册和套餐目录共用按唯一套餐代码创建的逻辑，保留已有套餐配置。[修复前记录](../../tmp/org-bootstrap-hh0f8p91/org-bootstrap-result.json)

SQLite 和已迁移的 PostgreSQL 上，两 API 同时注册两个新账户，各得到独立的个人组织、管理员成员和免费订阅。会话使用各自的真实登录凭据。老账户并发补建时，两边都返回同一组织，数据库只保留一个管理员成员和一条生效订阅。停止并重启 API 后重新登录，组织 ID 和订阅仍可读回。跨组织读取账单返回 403。PostgreSQL 临时库已删除。[SQLite](../../tmp/org-bootstrap-vb8v065d/org-bootstrap-result.json) · [PostgreSQL](../../tmp/org-bootstrap-egpfy57f/org-bootstrap-result.json) · [临时数据库](../../tmp/terminal-agent-postgres-0o0lgqut/cleanup.json)

实际 Edge 显示个人组织的免费订阅及 `free`、`pro`、`team` 套餐目录。同轮通过组织邀请、席位和角色回归，以及 API 的演示套餐切换、续期和取消。真实模型和模拟模型调用均为 0。生产支付由 FF-J06 继续验收。[Edge 操作](../../tmp/org-members-dual-api-p0l4pj2x/browser.log) · [截图](../../tmp/org-members-dual-api-p0l4pj2x/org-members.png) · [同轮 API 与清理](../../tmp/org-members-dual-api-p0l4pj2x/org-members-result.json)

复跑入口 [组织初始化验收](../server/org_bootstrap_acceptance.py)，默认 SQLite，添加 `--postgres` 切换到隔离 PostgreSQL。实现使用现有表和唯一约束，回退可恢复 `quota.py` 与 `org.py` 对应改动。
