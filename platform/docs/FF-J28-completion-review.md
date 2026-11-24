# FF-J28 S3 范围读取、HEAD 与签名下载

2026-11-19，普通 `/raw` 改为按块暂存，私有桶签名和文件系统同源下载均完成浏览器复验。[当前实现与复验](raw-download-capacity.md)

2026-11-18，FF-J28（WP16 / R6，T10）已通过。平台文件接口、私有 SeaweedFS 4.52 对象存储和生产构建的 Chromium 编辑器均经过实际下载。验收使用独立端口、临时数据库及测试桶。

此前，二进制文件接口先签名，再单独查询逻辑文件大小。两次查询之间若文件被替换，返回的大小可能属于另一个版本。现在一次选定物理对象，用其 HEAD 核对长度与 ETag，再对该对象签名。存储返回有效版本 ID 时，签名也绑定版本 ID。编辑器只向同源平台的算例 raw 路径发送平台令牌；跨源签名下载使用链接自身的授权。

| 验收范围 | 实际结果 |
|---|---|
| 平台 GET 与 HEAD | 完整内容、封闭区间、开放区间及后缀区间的长度与原件一致；HEAD 正文为空。多段 GET 返回两个指定片段。无效或越界范围返回 416。 |
| 私有桶直接下载 | 签名 GET 有效，完整正文和 3 种 Range 均逐字节核对。带无关 Bearer 仍可使用有效签名，移除签名的同一对象返回 403。签名 HEAD 返回正确长度和空正文，越界 GET 返回 416。 |
| 权限与缺失 | 匿名请求和其他用户不能通过平台接口取得文件或签名。缺失文件返回 404；携带旧文件版本读替换后的文件返回 409。 |
| 文件替换与重启 | 在选中旧对象后实际替换逻辑文件，原响应的长度与旧签名正文相符。终止并重启 API 后，新下载取得新内容，原签名仍取得旧内容。另验证开启桶版本控制的旧式物理键，两次覆盖前后返回各自版本。 |
| 浏览器 | 从工作区树打开二进制文件并点击下载，保存名称和 SHA-256 均正确。Chromium 的请求头记录显示，S3 未收到平台 Bearer；本地文件后端的同源 raw 接口收到 Bearer。验收记录只保存是否携带凭据。 |

[SQLite 与私有 S3](../../tmp/live-cli-startup-dwaz3oo6/file-download.json) · [PostgreSQL 与启用版本控制的私有 S3](../../tmp/live-cli-startup-sowo_p_x/file-download.json) · [文件系统兼容与浏览器回执](../../tmp/live-cli-startup-v8ann8gw/file-download.json)

平台目录和权限由平台 API 管理。直接访问签名 URL 时，浏览器须能访问签名使用的对象存储端点；跨源 fetch 须由该端点允许。没有原生版本控制的旧式可变物理键沿用对象存储自身的覆盖语义。已迁移的逻辑文件使用不可变物理键。平台完整 raw GET 仍会在服务端组装正文。

复验使用服务器虚拟环境执行 `live_llm_file_download_acceptance.py --source-profile-id 26 --s3`，增加 `--postgres` 验证开启原生版本控制的桶；省略 `--s3` 验证本地兼容。浏览器使用 `VIBEFLOW_TEST_WEB_DIST` 指定的生产构建和 `VIBEFLOW_TEST_BROWSER_PYTHON` 指定的 Playwright 环境。各组合均在真实 DeepSeek 工具回读后退出。回退应用和前端可使用前一构建，签名与读取改动没有数据库迁移。[验收入口](../server/live_llm_file_download_acceptance.py) · [PostgreSQL 模型回执](../../tmp/live-cli-startup-sowo_p_x/live-result.json)
