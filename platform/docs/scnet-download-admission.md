# SCNET 远程文件及目录 ZIP 下载

网页「下载」调用 `POST /api/v1/scnet/files:download`。接口沿用组织管理员校验和原远端请求签名。普通文本按 UTF-8 保存，带 `/` 的目录路径按既有协议逐块解码 Base64 ZIP，检查 ZIP 头尾后发送附件。小文本仍可单独预览，旧 JSON 接口的整个远端响应上限为 1 MiB。 [接口实现](../server/app/routers/scnet.py) · [远端读取](../server/app/services/scnet_download.py) · [网页下载](../web/src/panels/ScnetPanel.tsx)

单个二进制下载的解码上限为 100 MiB，远端 JSON 字节也有独立上限。超过 1 MiB 后暂存到节点磁盘，按新增空间分段准入。客户端断开或请求到期时等待已开始的读取退出，再关闭暂存并释放跨进程归档名额。慢读取期间释放数据库连接，发送附件前再次确认用户有效且仍有管理员权限。网页在浏览器收到受限附件后交给浏览器保存。 [归档准入](../server/app/archive_admission.py) · [磁盘暂存](../server/app/services/backup_download.py)

本机签名 HTTP 服务发送相同的 41,943,168 B ZIP。独立复现旧整包读取和 JSON 解析的 Python 分配峰值为 170,922,163 B，新接口在 SQLite 和 PostgreSQL 隔离服务中分别为 2,949,091 B 和 3,180,785 B。Edge 实际保存 `导出.zip`，源、API 和浏览器文件的 SHA-256 相同。这些数值测量 Python 分配。 [旧整包对照](../../tmp/scnet-download-acceptance-6l5n7ymu/whole-response-comparison.json) · [SQLite 及 Edge](../../tmp/scnet-download-acceptance-6l5n7ymu/download-result.json) · [PostgreSQL](../../tmp/scnet-download-acceptance-9o6pnppn/download-result.json) · [网页回执](../../tmp/scnet-download-acceptance-6l5n7ymu/browser-download.png)

两库在远端正文暂停时仍可提交独立数据库写入，实测分别约 47 ms 和 15 ms，第二个大包请求返回 429。节点不足返回 409，过大远端响应返回 413，无效 Base64、截断 JSON 及缺少 ZIP 尾部返回 502。实际断连及 504 后额度均归零，后续下载通过。本地 ZIP 导入、备份及恢复 18 项兼容检查通过。两次早期测试分别在传输端暂停后过早检查磁盘预留、在客户端读完后过早检查清理，最终以 API 的实际准入及排空状态核验。模型和模拟调用均为 0。 [旧归档兼容](../../tmp/archive-capacity-tvsnj6_c/checks.json) · [阶段汇总](../../tmp/N4-05-scnet-download-20261121/validation.json)

该结果验证本机签名协议、实际 HTTP、两种数据库与 Edge。授权 SCNET 环境验证、大文件上传和断点续传、完整覆盖保护继续归 FF-I13。RB-12 与 RB-14 继续覆盖其余长文件及恢复入口。
