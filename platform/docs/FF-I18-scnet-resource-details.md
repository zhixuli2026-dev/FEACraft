# FF-I18 SCNET 资源详情

工作台从资源行打开计算作业、历史作业、容器、Notebook 和远程桌面的详情。基本信息与资源字段使用中文标签，其他字段保留上游原名。基本信息、资源与运行、其他字段可独立折叠。字段名、原名和可见内容均可搜索，清空搜索后恢复折叠状态。复制单项或摘要使用服务端脱敏值。窗口很窄时，字段名与值换行排列。 [工作台源码](../web/src/panels/ScnetPanel.tsx)

## 上游协议

- 实时作业读取 `/openapi/v2/jobs/{jobId}`。历史作业读取 `/openapi/v2/historyjobs/{jobmanagerId}/{jobId}`，列表有记账时间时随查询传入。 [实时作业详情](https://www.scnet.cn/ac/openapi/doc/2.0/api/jobmanager/query-job-detail.html) · [历史作业详情](https://www.scnet.cn/ac/openapi/doc/2.0/api/jobmanager/query-history-job-detail.html)
- 容器读取 `/openapi/v2/instance-service/{id}/detail`，Notebook 读取 `/openapi/v2/notebook/detail` 并传 `notebookId`。 [容器详情](https://www.scnet.cn/ac/openapi/doc/2.0/api/container/detail.html) · [Notebook 详情](https://www.scnet.cn/ac/openapi/doc/2.0/api/notebook/detail.html)
- 远程桌面从 `/openapi/v2/desktop/vnc` 返回的会话中按 `sessionId` 或 `id` 核对唯一记录，读取宽高及节点信息。 [桌面会话文档](https://www.scnet.cn/ac/openapi/doc/2.0/api/desktop/vnc-list.html)

工作台向已有操作路由传 `detail_view: true`。API 解析详情响应中的 `data` 对象，生成字段分组和可复制的纯文本。使用旧作业详情调用方式的客户端继续收到原有数据形状和状态值，返回值同样经过脱敏。服务端遮蔽已知请求凭据和嵌套密码字段，剥离访问链接的用户名、密码、查询参数及片段，保留可显示的普通字段。超过 100 个字段时优先保留已识别字段，并在页面提示截断。 [请求客户端](../server/app/services/scnet_client.py) · [详情模型](../server/app/services/scnet_detail.py)

## 本机验证

2026-11-25，在独立 SQLite 和 PostgreSQL API 上通过签名回环 HTTP 验证了 5 类详情。覆盖历史查询时间、Notebook 参数、桌面唯一记录、旧响应形状、嵌套凭据、相对访问链接及超过 100 个字段。无效资源编号在发送上游请求前被拒绝。临时 PostgreSQL 已核验删除。 [验收入口](../server/scnet_detail_acceptance.py) · [SQLite](../../tmp/scnet-detail-acceptance-g4nmwiz1/detail-result.json) · [PostgreSQL](../../tmp/scnet-detail-acceptance-bwbp612z/detail-result.json) · [临时库清理](../../tmp/mixedread-postgres-6l8wn5rw/cleanup.json)

Edge 通过真实生产构建打开上述五类详情，验证搜索命中与无结果、键盘折叠、切换详情后重置搜索、复制单项和摘要、桌面分辨率及关闭详情。剪贴板按实际复制结果核对脱敏。检查了 730 和 390 像素视口，窄屏详情无横向溢出。TypeScript 和 Vite 生产构建通过。FF-I20 端点诊断回归通过。模型调用 0 次。 [作业界面](../../tmp/scnet-detail-acceptance-g4nmwiz1/detail-jobs-730.png) · [窄屏桌面详情](../../tmp/scnet-detail-acceptance-g4nmwiz1/detail-desktop-390.png) · [FF-I20 回执](../../tmp/ff-i20-scnet-http-t7q1ni_p/scnet-diagnostics.json)

构建前后 C 盘可用为 40,718,471,168 B 与 40,720,199,680 B。Docker 构建缓存保持 5.819 GB，本阶段回收 0 B。原 11 个运行容器身份核对一致。 [宿主机盘占用](../../tmp/scnet-detail-acceptance-g4nmwiz1/disk-after.json) · [Docker 占用](../../tmp/scnet-detail-acceptance-g4nmwiz1/docker-after.txt)

签名回环服务用于核对公开协议和浏览器交互。SCNET 云端账户、真实各区域的完整字段以及资源列表和调度器联调仍属 T09 验收。FF-I18 保持实施中。 [全功能计划](full-function-delivery-plan.md#49-scnet-hpc-与高级远程资源)
