# FF-J05 组织成员、角色与席位

2026-11-21，本机 SQLite、迁移到当前版本的临时 PostgreSQL 和真实 Edge 验收通过。免费组织有两个席位。邀请占用席位，接受沿用原席位，拒绝、撤销及禁用释放额度，重新启用时重新检查。受邀用户的个人组织仍可独立使用。

邀请、成员变更与套餐席位检查使用同一组织行的数据库写事务。跨两个真实 API 进程并发申请最后一个席位时，一个返回 201，另一个返回 429。两个管理员分别修改对方角色时，一个成功，被撤销管理员角色的一方返回 403，最终保留一个管理员。非成员管理、无权限代接受、重复邀请、无效目标和自我禁用沿用既有状态码。

Edge 在 PostgreSQL API 上完成管理员邀请、受邀账户接受、改为 viewer 和禁用，席位从 2/2 回到 1/2。控制台读取了角色变更审计。SQLite 和 PostgreSQL 均运行成员 API 与现有演示套餐回归，两轮的隔离 API 进程均已退出，PostgreSQL 临时库已移除。此项验收包含组织成员和席位控制台。组织资源 ACL、生产账单和跨进程审计链分别按 FF-J07、FF-J06 和 FF-J11 交付。本轮模型调用 0，模拟模型调用 0。

复跑入口 [成员 API](../server/org_members_smoke.py) 与 [浏览器验收](../tools/e2e-org-members.mjs)。成员 API 可指定同一临时数据库的两个 API 地址 `VIBEFLOW_TEST_BASE` 和 `VIBEFLOW_TEST_PEER_BASE`。浏览器用 `--members-only` 验证组织面板。回退只涉及路由代码，无数据库迁移或旧记录改写。多进程部署回退前先切回单 API 写入。

实际证据 [SQLite 成员 API](../../tmp/org-members-dual-api-xrbrhyp_/membership.log) · [SQLite 演示套餐](../../tmp/org-members-dual-api-xrbrhyp_/billing.log) · [PostgreSQL 与进程回收](../../tmp/org-members-dual-api-kivawr4a/org-members-result.json) · [Edge 操作](../../tmp/org-members-dual-api-kivawr4a/browser.log) · [控制台截图](../../tmp/org-members-dual-api-kivawr4a/org-members.png)。旧组合脚本的 SQLite 浏览器轮次停在运行时监控检查。新版成员专项浏览器轮次通过。[旧组合结果](../../tmp/org-members-dual-api-xrbrhyp_/browser.log)
