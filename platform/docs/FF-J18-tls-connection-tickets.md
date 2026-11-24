# FF-J18 HTTPS、连接票据和代理日志

浏览器先向 `POST /api/v1/connection-tickets` 请求授权。事件流、CAD 和终端票据有效 60 秒，绑定连接类型和算例或终端编号。重连重新签发。可视化使用有效 300 秒、限定 `/viz-proxy/{id}/` 的 HttpOnly Cookie，HTTPS 下带 `Secure`，iframe 地址只含路径。REST API 和程序化 WebSocket 注入标准 `Authorization` 请求头。可视化实例间转发使用单会话票据，渲染器收到的请求移除授权头、Cookie 和票据参数。 [签发接口](../server/app/routers/connection_tickets.py) · [可视化网关](../server/app/routers/vizproxy.py)

生产 Nginx 的访问日志仅记录路径，省略查询参数和 Referer。升级连接及可视化代理的错误日志只保留严重级别，避免上游连接错误记录原始请求行。API 关闭 uvicorn HTTP 访问日志。 [代理配置](../infra/nginx-web.conf) · [Nginx 日志格式](https://nginx.org/en/docs/http/ngx_http_log_module.html)

## 实际验收

2026-11-24 使用独立 Nginx、Linux API、SQLite 和 Docker 本地卷。最终 API 和 Web 镜像使用自身打包的应用，TLS 使用隔离证书。Python 客户端核验证书及主机名并协商 TLS 1.3，Edge 仅信任本次验收证书公钥。首页和 API 安全响应头、允许及拒绝来源的 CORS 预检均通过。长期账户 JWT 放入查询参数、票据跨算例或跨路由、过期、签名修改及启动票据挂接已有终端均被拒绝。另一个账户无法签发票据或读取可视化，连接票据不能用于通用 REST。验收日志中发现 0 个测试凭据。 [最终回执](../../tmp/ff-j18-tls-urs9u4tj/acceptance.json) · [验收入口](../server/connection_ticket_tls_acceptance.py)

Edge 成功连接事件、CAD、终端和 VTK 会话，实际渲染 STL 三角面。终端执行真实 Bash 命令、断线后凭会话票据接回，浏览器另建并关闭一条终端，API 核验进程范围清空。首次 Linux 启动缺少 EGL，补齐 Debian 签名的 Mesa EGL 运行库后通过。可视化关闭同样核验进程范围为空。这轮基础设施验收模型调用 0 次。 [实际渲染](../../tmp/ff-j18-tls-urs9u4tj/tls-viz.png) · [渲染器启动记录](../../tmp/ff-j18-tls-urs9u4tj/renderer.log) · [EGL 修复前记录](../../tmp/ff-j18-tls-rf2gkp20/renderer.log)

## 部署配置

将 `fullchain.pem` 和 `privkey.pem` 放在 `TLS_CERT_DIRECTORY`，将 `WEB_ORIGIN` 设为对外 HTTPS 起源。通过 `docker-compose.prod.yml` 和 `docker-compose.tls.yml` 同时加载 HTTP 与 HTTPS 配置，默认发布 HTTPS 443。只对外提供 HTTPS 时可将 `WEB_HTTP_BIND` 设置为 `127.0.0.1:8080`。验收在本机使用临时证书，正式公网证书与 DNS 由目标环境提供。 [Compose 覆盖](../infra/docker-compose.tls.yml) · [Nginx HTTPS 设置](https://nginx.org/en/docs/http/configuring_https_servers.html)
