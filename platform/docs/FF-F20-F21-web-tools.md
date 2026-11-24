# FF-F20 和 FF-F21 网页工具

`web_search` 使用配置的搜索服务，未配置时使用 Crossref。同一 Agent 回合的搜索共用预算，不同回合独立计数，结束后移除运行状态。`web_fetch` 固定连接到已校验的公网 DNS 地址，每次重定向都重新验证，保持原域名 TLS 验证。正文按分段读取，接受的总大小不超过 8 MiB，多个重定向共用总期限。请求取消会等待底层任务退出。 [搜索实现](../server/app/services/websearch.py) · [抓取实现](../server/app/services/webfetch.py) · [Crossref API](https://www.crossref.org/documentation/retrieve-metadata/rest-api/)

- 实际 DeepSeek 选择 `web_search` 与 `web_fetch`，真实 Crossref 返回论文 `10.1063/1.168744`，公开 HTTPS 正文取得 HTTP 200。随后在另一回合，真实工具入口拒绝隔离 API 的回环地址。工具记录持久化，无关账号读取返回 404。 [真实回执](../../tmp/live-f20-f21-web-tools-1bpy8tse/live-result.json) · [原始 SSE](../../tmp/live-f20-f21-web-tools-1bpy8tse/actual-public-tool-sse.json) · [论文出版页](https://pubs.aip.org/aip/cip/article/12/6/620/510187/A-tensorial-approach-to-computational-continuum)
- 另一实际模型回合选中 Crossref 搜索。验收在真实 Crossref 响应到达后暂缓把结果交给 Agent，通过正式取消接口终止。收到 `tool.result cancelled` 和 `run.cancelled`，后台任务退出。 [真实取消](../../tmp/live-f20-f21-web-tools-chh6xe92/live-result.json)
- 独立 HTTP 测试覆盖分段正文的最后一块、无 `Content-Length` 超过 8 MiB、压缩拒绝、回环 DNS 及重定向拒绝。生命周期检查覆盖外层取消清理、共享预算、异会话运行 ID 拒绝和完成后的移除。 [协议入口](../server/web_tool_transport_smoke.py) · [协议回执](../../tmp/FF-F20-F21-web-tools-20261124/transport.json)

通过组收到 5 次真实 `deepseek-flash` HTTP 200，模拟 0。Windows 和 Docker 的默认直连探测在本机曾超时。隔离验收仅对 Crossref 使用本机配置的 DNS 服务器实际返回且经直连验证的公开 A 记录，应用的公网地址检查、直接连接和域名证书校验照常执行。 [真实模型入口](../server/live_llm_web_tools_acceptance.py)
