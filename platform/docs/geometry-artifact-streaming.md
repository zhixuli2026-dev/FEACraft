# N4-05 几何发布原件下载

`GET/HEAD /api/v1/cases/{id}/geometry/artifact` 保留已发布 STEP、STP 与 STL 的路径约束。入口在算例协调区内固定账户、发布记录和原作业，随后释放数据库事务。历史文件和 `X-Vibeflow-File-Version` 绑定同一个不可变清单。

下载复用历史 LaTeX 原件的分块验证。每次校验全部目标文件和发布清单范围，再返回 HTTP 响应。Range 仍完整读取目标以证明 SHA-256，只暂存要求返回的区间。完整 GET、单段及有序多段响应统一复用节点预留、下载线程和断线后的 ASGI 清理。

验收通过公开几何 API 读取真实文件系统和带认证的真实 SeaweedFS 中的二进制 STL，使用生产发布封装、原清单摘要和隔离 SQLite。夹具生成已核验结构的 40 MiB STL 和一个较新的小版本，用于验证历史选择、共用 HTTP 边界和同算例并发写入。CadQuery 生成与工程验证有独立入口。[几何复验入口](../server/geometry_artifact_streaming_smoke.py) · [共用能力的历史 PDF 复验](latex-artifact-streaming.md)

## 2026-11-19 实测

| 后端 | 41,943,084 B 历史 STL 的 HEAD 峰值 | 完整 GET 峰值 | 单段 206 峰值 | 回执 |
| --- | ---: | ---: | ---: | --- |
| Windows 文件系统 + SQLite | 237,156 B | 185,657 B | 181,658 B | [文件系统](../../tmp/geometry-artifact-fs-kzs15aho/geometry-artifact-streaming.json) |
| 带认证的 SeaweedFS 4.52 + SQLite | 213,149 B | 483,408 B | 212,293 B | [S3](../../tmp/geometry-artifact-s3-vdbhx2lg/geometry-artifact-streaming.json) |

完整 GET 校验了历史版本的 SHA-256，旧版和当前版的 HEAD 返回各自长度。单段、多段与 416 保留协议语义，错误版本返回 409，隐藏产物及跨账户访问返回 404。每个历史响应只启动一条目标文件流，每块最多 65,536 B。S3 对目标执行一次条件 GET，本地未物化整个发布目录，小 Range 的磁盘预留为 0。

两组在真实校验读取暂停期间成功保存同一个算例中的另一个文件。原件在所请求区间之外发生同尺寸损坏时，完整 GET 和 Range 均返回 `evidence_invalid` 409，并释放暂存。实际客户端断线前，两组观察到 41,943,084 B 预留，断线后归零。表中是未并发保存时的单次准备阶段 Python 分配峰值。

通过审查的回归顺序运行几何和原历史 LaTeX 套件，两套均通过。LaTeX 复验也分别在实际文件系统和认证 SeaweedFS 上确认原历史/当前版本、HTTP 和失败清理。四个隔离 API 的原进程与两个对象存储均已退出，模型调用和模拟调用为 0。测试文件保存在 D 盘，C 盘前后可用空间均约 22.22 GiB。[回归汇总](../../tmp/vibeflow-regression-all-20261119-123122-2d0ada/summary.json) · [LaTeX 文件系统复验](../../tmp/latex-artifact-fs-3nez255b/latex-artifact-streaming.json) · [LaTeX S3 复验](../../tmp/latex-artifact-s3-jyvyr7jw/latex-artifact-streaming.json)
