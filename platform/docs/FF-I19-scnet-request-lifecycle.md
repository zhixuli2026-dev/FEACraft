# FF-I19 SCNET 请求确认与取消

工作台在提交、创建、删除、终止及其他资源变更前显示动作和目标，可能产生费用的动作在确认框中标明。仅在确认后发送变更请求。同一工作台一次保持一个活动请求。连接、刷新、切换栏目和重新登录使用读请求。 [确认与请求状态](../web/src/panels/ScnetPanel.tsx)

停止等待会取消浏览器请求，应用收到断开后停止并等待上游 HTTP 客户端退出。远端已受理的变更仍按远端状态判断。停止等待、变更结果未知或成功后刷新失败时，相关栏目暂时禁用变更，成功刷新资源列表后恢复。切换栏目和区域或注销会使旧响应失效。区域变化清除令牌并回到连接状态，注销清除页面内的 Access Key、Secret Key 和令牌。 [应用请求生命周期](../server/app/routers/scnet.py) · [断开处理](../server/app/services/request_lifecycle.py)

读取和下载可跟随最多三次同源重定向，校验协议、主机和有效端口，签名请求各跳均丢弃上游 Cookie。HTTPS 用于远端服务，回环 HTTP 用于本机验收。变更请求收到重定向即返回待核对状态。JSON 请求从发送至读取完整响应共享十秒时限，下载沿用原有流式时限和额度。 [签名客户端](../server/app/services/scnet_client.py) · [下载入口](../server/app/services/scnet_download.py) · [HTTPX 超时说明](https://www.python-httpx.org/advanced/timeouts/)

## 本机验收

- 签名 HTTP 服务核对确认前发送 0 次。同源读取与两种下载入口均读回实际结果，异源目标收到 0 次请求，循环达到三跳上限。上游返回 Cookie 时，下一跳未携带 Cookie。变更遇到重定向和持续分段返回超时，分别只收到一次变更。后者在 10.016 秒返回超时。 [请求回执](../../tmp/scnet-request-acceptance-tmeqa3qf/request-result.json)
- 生产构建与实际 Edge 验证确认框取消发送 0 次，正常提交发送 1 次。另一笔提交停止等待时，上游连接实际关闭，刷新后仍读到先前受理的作业，变更按钮在刷新成功前维持禁用。切换栏目取消未返回详情，切换区域取消未返回的连接，注销清除页面凭据。共核验 3 次上游连接退出。 [730 像素](../../tmp/scnet-request-acceptance-tmeqa3qf/scnet-stop-waiting-730.png) · [390 像素](../../tmp/scnet-request-acceptance-tmeqa3qf/scnet-stop-waiting-390.png)
- 五类详情回归在 PostgreSQL 和实际 Edge 上通过。旧端点诊断通过。原 40 MiB ZIP 经 Edge 下载后 SHA-256 一致，实际客户端下载断开后额度归零。所有回归均通过模型使用审查，模型调用 0 次。 [详情 PostgreSQL](../../tmp/scnet-detail-acceptance-ugbt24p3/detail-result.json) · [详情 Edge](../../tmp/scnet-detail-acceptance-js27ro2x/detail-result.json) · [端点诊断](../../tmp/ff-i20-scnet-http-tfiln6zw/scnet-diagnostics.json) · [下载回归](../../tmp/scnet-download-acceptance-mvhwyd7a/download-result.json)

C 盘可用空间由 40,719,368,192 B 变为 40,715,689,984 B。Docker 构建缓存保持 5.819 GB，本阶段回收 0 B。原 11 个运行容器身份核对一致。 [Windows 盘占用](../../tmp/ff-i19-scnet-20261125/disk-after.json) · [Docker 占用](../../tmp/ff-i19-scnet-20261125/docker-after.txt)

回执来自隔离签名服务和浏览器。正式 SCNET 的授权区域、账户续约、远端取消及计费仍待 T09 联调。 [验收入口](../server/scnet_request_acceptance.py) · [交付计划](full-function-delivery-plan.md#49-scnet-hpc-与高级远程资源)
