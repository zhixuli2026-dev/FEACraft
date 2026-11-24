# FF-I20 SCNET 服务地址和故障诊断

作业和桌面请求使用 HPC 根地址，容器和 Notebook 可用 `ai_base_url` 选独立 AI 根地址。只有根地址尚无路径时才补 `/hpc` 或 `/ai`，已配置的前缀和自定义代理路径继续有效。服务地址拒绝缺少主机名、无效端口、内联凭据和查询参数，回环 HTTP 用于本地协议验收。 [授权区域地址字段](https://www.scnet.cn/ac/openapi/doc/2.0/api/safecertification/get-center-info.html) · [容器地址](https://www.scnet.cn/ac/openapi/doc/2.0/api/container-manager.html) · [桌面地址](https://www.scnet.cn/ac/openapi/doc/2.0/api/desktop-manager.html)

代理将上游状态、`msg` 或 `message` 和业务错误码一起返回。HTTP 200 中明确的业务失败也进入错误路径。诊断只暴露各请求头、token 和请求头合计的字节长度，错误地址去掉查询参数，响应内容遮蔽凭据。资源标识作为单一路径段编码。 [地址及错误解析](../server/app/services/scnet.py) · [请求客户端](../server/app/services/scnet_client.py)

2026-11-24 隔离平台和独立回环 HTTP 服务通过 HPC、AI、已有路径、不同 AI 根、六种坏地址、401 拒绝、签名校验、400/431/494、HTTP 200 应用错误和正常重试验收。HMAC、Windows DPAPI、详情脱敏和同源重定向专项通过。本项模型调用 0，SCNET 云端账户和各资源操作按其独立交付项验收。 [实际 HTTP 回执](../../tmp/ff-i20-scnet-http-nj87z0wh/scnet-diagnostics.json) · [验收入口](../server/scnet_endpoint_diagnostics_acceptance.py)
