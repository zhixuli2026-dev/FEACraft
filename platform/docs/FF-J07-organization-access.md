# FF-J07 组织级工作区和算例授权

资源所有者可向多个组织分别授予工作区级或算例级访问。工作区授权可继承到下属算例，单算例授权只显示可访问的算例。组织成员的只读角色会限制实际权限。所有者可委托某组织的管理员管理该组织的单条授权，管理范围和最高角色由所有者指定。授权修改带版本校验和审计记录。旧版工作区共享仍可读取，在首次修改工作区授权时转入数据库。 [授权服务](../server/app/services/sharing.py) · [迁移](../server/migrations/versions/9b14c020b4d8_organization_resource_grants.py)

桌面和手机均可从工作区的「组织授权」设置授权范围、组织、角色和启用状态。所有者可修改授权和委托。代理管理员可在委托范围内保存或撤销。只读算例继续提供文件、搜索及已有结果。采用已支持的 API 和数据库授权，生产部署执行 Alembic 迁移后启用。现行开发服务未迁移。本次验收使用独立数据库及服务。

## 验收

- SQLite 和 PostgreSQL 均通过上一迁移版本到当前版本的升级，校验了联合主键、三个约束和外键。SQLite 完整性检查通过，PostgreSQL 临时库已删除。模型调用 0 次。 [迁移验收](../../tmp/ff-j07-acl-migrations-x710j4u4/migration-result.json)
- 两 API 上以实际账号核验旧共享导入、两个组织并存、组织内查看角色、代理权限上限、版本冲突、越权写入和其他算例隐藏。读取覆盖算例文件、事件、MPI、作业、报告、PPT、LaTeX、进程和会话，工作区搜索不会返回私有算例。撤销后已建立的 WebSocket 收到 4403。独立 Redis/arq worker 重新检查授权，撤销后的排队 Windows 命令没有执行，恢复授权后同一作业完成。 [PostgreSQL](../../tmp/live-j07-acl-primary-eziyxplu/organization-acl.json) · [SQLite](../../tmp/live-j07-acl-primary-zb3kxz75/organization-acl.json)
- 两轮最终验收各发出一次真实 DeepSeek `deepseek-flash` 流式请求，两次均为 HTTP 200，模拟 0 次。授权成员能读取实际会话，组织授权撤销后返回 404。共享会话和导出不包含单独写入的私人 ACP 审批细节。补测中另有四次真实 HTTP 200，分别用于最初 PostgreSQL 验证及补齐流式分片、浏览器断言。 [首次 PostgreSQL](../../tmp/live-j07-acl-primary-o4yawqhd/organization-acl.json) · [流式补测](../../tmp/live-j07-acl-peer-gvsqutut/upstream.jsonl) · [浏览器补测一](../../tmp/live-j07-acl-primary-1aucu7e8/organization-acl.json) · [浏览器补测二](../../tmp/live-j07-acl-primary-a7hscxza/organization-acl.json)
- Edge 在 1440px 实测所有者保存算例授权。390px 下实测受委托管理员只能维持只读权限，可撤销授权。撤销后重新加载，工作区在实际列表响应及界面中消失。移动面板无横向溢出。浏览器动作本身触发模型调用 0 次。 [浏览器结果](../../tmp/live-j07-acl-primary-zb3kxz75/browser.json) · [桌面](../../tmp/live-j07-acl-primary-zb3kxz75/acl-owner-1440.png) · [移动](../../tmp/live-j07-acl-primary-zb3kxz75/acl-delegate-390.png)
