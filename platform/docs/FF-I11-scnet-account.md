# FF-I11、FF-I17 SCNET 账户与区域接入

2026-11-25，用户将 FF-I11 至 FF-I21 移出本次交付。以下记录供追溯。 [当前范围](full-function-delivery-plan.md#21-最新范围规则)

管理员在「连接与概览」填写 SCNET 用户名、Access Key 和 Secret Key。服务端按推荐的 `POST /api/user/v3/tokens` 协议，把 `accessKey`、秒级 `timestamp` 和 `user` 编为紧凑 JSON，使用 Secret Key 计算 HMAC-SHA256。浏览器只收到站内会话编号、版本、账户概览和区域服务状态。区域 `0` 的凭证读取平台账户概览，其余凭证各自查询对应区域。发现的计算、AI、文件和终端服务按授权区域显示。 [认证](https://www.scnet.cn/ac/openapi/doc/2.0/api/safecertification/get-user-tokens-aksk.html) · [账户](https://www.scnet.cn/ac/openapi/doc/2.0/api/userresource/get-common-user.html) · [区域](https://www.scnet.cn/ac/openapi/doc/2.0/api/safecertification/get-center-info.html)

区域令牌与服务地址存于加密会话，按站内用户隔离。部署副本共用数据库及现有 vault 密钥。登录完成后服务端只保存区域凭证，清除页面中的 AK、SK 和手填 Token。授权区域支持手动续约。服务端先锁定当前会话版本再发续约，更新成功后旧版本失效。注销删除站内会话，与进行中的续约竞态也不会恢复已删除的记录。SCNET 公开协议提供凭证续约，本站注销的作用范围为本站。 [续约](https://www.scnet.cn/ac/openapi/doc/2.0/api/safecertification/renewal-token.html)

账户连接目前覆盖实时作业列表、作业详情和远程桌面列表及详情。实时作业读取区域返回的首个有效调度器，首批最多 25 条，再由界面分页。其余资源操作由后续 FF-I12 至 FF-I16 接入各自官方接口。生产数据库随标准发布流程执行 `alembic upgrade head`，新修订为 `f8c3d9486581`。默认认证地址为 `https://api.scnet.cn`，平台地址为 `https://www.scnet.cn`。 [调度器](https://www.scnet.cn/ac/openapi/doc/2.0/api/jobmanager/list-cluster.html) · [实时作业](https://www.scnet.cn/ac/openapi/doc/2.0/api/jobmanager/query-job.html) · [桌面列表](https://www.scnet.cn/ac/openapi/doc/2.0/api/desktop/vnc-list.html) · [数据库迁移](../server/migrations/versions/f8c3d9486581_scnet_account_sessions.py)

## 本机验收

- SQLite 和 PostgreSQL 均通过实际升级、降级、再次升级。签名回环 HTTP 验证错误密钥、畸形请求的脱敏、仅持久化加密区域凭证、区域选择、其他用户无法查询或续约、旧版本失效，以及续约并发遇到注销时会话保持失效。临时 PostgreSQL 库已删除。 [SQLite 回执](../../tmp/scnet-account-acceptance-826djdho/account-result.json) · [PostgreSQL 回执](../../tmp/scnet-account-acceptance-9_r676_m/account-result.json) · [数据库回收](../../tmp/mixedread-postgres-8q1s6vy7/cleanup.json)
- Edge 用生产前端构建完成登录、切到南区查询并打开作业详情、切回北区续约、用新凭证查询桌面、注销及旧引用失效。检查 730 和 390 像素界面，设计检测没有发现项。手动连接、变更确认及下载请求回归通过。 [730 像素](../../tmp/scnet-account-acceptance-826djdho/account-730.png) · [390 像素](../../tmp/scnet-account-acceptance-826djdho/account-390.png) · [既有请求回归](../../tmp/scnet-request-acceptance-tbqajo97/request-result.json)
- 模型调用 0 次。11 个原运行容器身份一致，C 盘约 40.7 GB 可用，本轮 Docker 缓存回收 0 字节。 [磁盘与 Docker 回执](../../tmp/scnet-account-acceptance-826djdho/disk-docker.json)

T09 继续保留授权 SCNET 云端账户、实际区域资源、上游过期及续约行为验收。原版启用证据和其余资源接口仍在原计划。当前进度为主线 51/53、全功能 269/295。
