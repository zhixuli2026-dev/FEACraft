# FF-H17 大文件分块上传、断点续传与多段 Range

2026-11-20，S3 实际分片正文、描述记录和进度传输移出数据库写事务。认证 SeaweedFS 与 SQLite、PostgreSQL 通过同账户并发上传、有限预留、API 退出后同偏移复用及 504 后续传。使用当前工作区的构建复验真实 Chromium 大文件恢复与多段 Range。[S3 分片实测](upload-chunk-credit.md#2026-11-20-s3-分片正文写入)

2026-11-20，S3 上传取消在 SQLite、PostgreSQL 上补齐真实并发写入和 API 退出恢复。浏览器重新验证 17,825,805 B 的创建和分块丢失应答、续传与多段 Range。按创建键取消能够恢复尚未建立远端元数据的上传，只有查看权限的用户取消请求被拒绝。[S3 上传取消与浏览器复验](upload-write-owner-capacity.md#2026-11-20-s3-上传取消)

2026-11-20，本地上传完成接入锁外正文校验与事务副本准备。修复了浏览器切换算例时同级组件重复键造成的重复控件。SQLite、PostgreSQL 生产构建用同一上传标识恢复创建和分块应答丢失，5 个分块共 17,825,805 B，已核对 HTTP Range 和最终摘要。旧兼容套件的 19 项通过。[本地完成与浏览器复验](upload-write-owner-capacity.md#2026-11-20-本地上传完成与后台清理)

2026-11-18 在目标 Windows 主机的隔离 API 和实际 Chromium 中验证了浏览器大文件上传。文件系统与私有 S3 分别搭配 SQLite 和 PostgreSQL，共四种组合。测试使用独立数据库和存储，浏览器加载前端生产构建。

## 用户能恢复什么

16 MiB 以内的文件走普通上传。更大文件使用平台的分块 API，单文件上限 1 GiB，API 每块最多 8 MiB，浏览器每次校验和发送 4 MiB。服务端保存上传偏移，完成时发布目标文件。浏览器持久化文件特征和进度；刷新后须重新选择内容相同的文件。最后一块已送达时可直接完成。

此前浏览器会保存一次选择对应的 UUID，但首次请求没有发送它。服务器已创建会话而应答丢失时，再试一次可能产生第二笔预留。现在创建请求携带同一个 `Idempotency-Key`。服务器将账户、算例、声明内容以及是否指定目标版本保存在可恢复的创建记录中。刷新后可按标识找回会话。同一标识携带不同声明会返回冲突。取消可先于创建抵达，同一标识随后抵达的创建也会被拒绝。

旧客户端仍可使用原来的无标识创建接口。已有服务端 ID 的旧记录按 ID 续传。旧版浏览器如果在保存服务端 ID 前丢失创建应答，需要用户结束本机记录，再在“上传恢复”中核对原会话。换设备或清除浏览器存储后也应核对服务端记录。[浏览器恢复](browser-upload-resume.md) · [上传归属修复](upload-owner-reconciliation.md)

## 实测结果

| 场景 | 观察 |
|---|---|
| Chromium 在服务端创建成功后丢失应答，再在首块写入后丢失应答 | 两次重新加载后沿用同一上传。17,825,805 字节文件只创建一次，五个请求偏移依次为 0、4,194,304、8,388,608、12,582,912、16,777,216。下载文件的 SHA-256 与选中的原文件一致。 |
| 同一声明并发到达、声明冲突、共享工作区 | 同一成员的并发创建返回同一 ID，只占一份声明额度。不同成员使用相同标识得到不同 ID，由项目所有者承担额度。无权访问的账户无法查询或取消，他人变为只读后也无法取消。 |
| 创建期间 API 退出 | 四组各终止实际 API 进程三次，分别在写入前、写入后继续，以及写入前退出后取消。已提交的预留可找回，结算后活动预留均为零。两种 S3 组合各有三次迟到的条件写入收到 412。 |
| 原始文件下载 | GET 完整文件、HEAD 空正文与准确长度、双区间 GET 返回 206 和原始片段，重叠范围返回 416。既有下载验收覆盖开放区间、后缀区间、签名访问及访问控制。 |

| 组合 | 上传与额度 | 真实模型回读 |
|---|---|---|
| SQLite / 文件系统 | [上传记录](../../tmp/live-cli-startup-k_n4oq9w/upload-creation.json) | [DeepSeek](../../tmp/live-cli-startup-k_n4oq9w/live-result.json) |
| PostgreSQL / 文件系统 | [上传记录](../../tmp/live-cli-startup-_76muyk5/upload-creation.json) | [DeepSeek](../../tmp/live-cli-startup-_76muyk5/live-result.json) |
| SQLite / 私有 S3 | [上传记录](../../tmp/live-cli-startup-5k4po6g2/upload-creation.json) | [DeepSeek](../../tmp/live-cli-startup-5k4po6g2/live-result.json) |
| PostgreSQL / 私有 S3 | [上传记录](../../tmp/live-cli-startup-5bd4hezi/upload-creation.json) | [DeepSeek](../../tmp/live-cli-startup-5bd4hezi/live-result.json) |

每组在文件和额度断言通过后，由配置 26 的 `deepseek-flash` 读取算例中的验收摘要凭据。四组共 8 次真实 HTTP 200。文件内容、权限、额度和故障恢复由实际 API、数据库、对象存储和浏览器分别验证。[上传验收入口](../server/live_llm_upload_creation_acceptance.py) · [范围与签名下载](FF-J28-completion-review.md) · [上传体与节点暂存](upload-node-ingress-interleavings.md) · [分块额度与清理](upload-chunk-credit.md)

## 复验与部署

使用服务器虚拟环境运行 `live_llm_upload_creation_acceptance.py --source-profile-id 26`。增加 `--postgres` 或 `--s3` 选择对应后端。`VIBEFLOW_TEST_WEB_DIST` 指向生产构建，`VIBEFLOW_TEST_BROWSER_PYTHON` 指向装有 Playwright 的 Python。兼容入口另见 [浏览器回归执行器](../server/live_llm_upload_browser_compat_acceptance.py)。

此次变更复用现有数据库表。发布时应先切换所有 API 写入进程，再提供新版前端。回退前应让正在创建的会话完成或取消，保留数据库创建记录和 S3 的零字节关闭标记，以免迟到的旧请求重新生成暂存。测试期间发现 Uvicorn 的 WebSocket 握手日志含隔离账户的短期登录令牌，已在日志入口移除 URL 查询串，并清理两份早期测试日志中的令牌。四组浏览器验收均检查握手仍有记录且不包含该次测试账户的令牌。

## 浏览器兼容与交付结论

三套浏览器回归分别通过 11、5、3 项检查。覆盖跨标签页取消、文件内容重核、提交回执和后续编辑、存储异常，以及窄屏操作。检验后续编辑前先读取当前版本、条件写入并验证成功。初次执行器创建了不能打开第二标签页的 Playwright 临时页面；改用共享浏览器上下文后，实际打开第二标签页复验。此组单独记录两次真实模型 HTTP 200。[逐项结果](../../tmp/live-cli-startup-o13_8ef9/upload-browser-compat.json) · [模型回执](../../tmp/live-cli-startup-o13_8ef9/live-result.json)

FF-H17 的原定范围已通过验收。开发中的 RB-12 仍覆盖其他文件、导出和暂存路径；主线 50/53、全功能 12/295、资源预算 12/17。
