# 算例修订历史的读取与并发保存

2026-11-21，N4-05 的 RB-12、RB-14 验收。公开 `GET /api/v1/cases/{id}/provenance` 在短事务内选定修订号、历史文件和字典版本，随后释放算例协调锁与存储额度。在现有有界读取线程中以最多 64 KiB 的块读取、校验哈希链与字典摘要，并在返回前重新确认两个文件版本。来源变化返回 409 `file_changed_during_read`。在截止时间到达后，先排空已开始的读取，再返回 504 `case_provenance_timeout`。[公开入口](../server/app/routers/workspaces.py) · [历史分页与校验](../server/app/services/provenance.py)

首屏保留当前页及其前一条记录，深页在同一版本上扫描两次，保留该页、用于判定变化的前一条记录和链头。需要完整校验链及计算总数，I/O 量仍随历史增长。读取缓冲的正文以最大单条修订为限，异常链保留校验问题。Windows 本地文件按块关闭句柄，限制持有原文件的时间。Windows 对尚有读取句柄的目标文件仍可能拒绝同路径替换。

| 验收 | 实测 | 证据 |
| --- | --- | --- |
| 修复前 | 实际读取 1,202,564 B 正文时，独立 SQLite 写事务无法取得写锁，普通 caseDict 保存等待。 | [基线](../../tmp/live-cli-startup-z48_vc3u/provenance-capacity-result.json) |
| 修复后 | Windows 文件系统配 SQLite、认证 SeaweedFS 配 PostgreSQL 的首块均为 65,536 B。暂停物理读期间，可正常获取独立 SQL 锁，同算例的另一文件实际保存成功。在正文排空与版本复核之间提交 caseDict，原查询均返回 409，新查询读回变更。 | [SQLite](../../tmp/live-cli-startup-zugnxaa4/provenance-capacity-result.json) · [PostgreSQL 与 S3](../../tmp/live-cli-startup-nc56nix7/provenance-capacity-result.json) |
| Windows 共享限制 | 早期复验在打开历史文件的同一时刻尝试替换它，Windows 拒绝替换，产品返回 409。最终范围读把句柄限制在一次分块读取，并在测试中分别核对正文读取时的独立写入与读完之后的同路径变更。 | [首次复验](../../tmp/live-cli-startup-4i5fur2v/live-result.json) |
| 请求超时 | SQLite 将隔离用例期限设为 4 秒。实际读取先排空，再返回 504，暂停期间 SQLite 写事务可取得锁。原历史仍可完整回读。 | [超时](../../tmp/live-cli-startup-h0xk6_8b/provenance-capacity-result.json) |
| 界面兼容 | 修订历史分页、保存后刷新、键盘展开、算例切换和旧列表回读均经真实 Edge 复验。 | [浏览器回执](../../tmp/case-intent-browser-w4levz1w/intent-roundtrip-result.json) |

显式验收入口为 `platform/server/live_llm_case_provenance_capacity_acceptance.py`。本阶段两组任务合计 20 次真实 DeepSeek HTTP 200，修订读取组 8 次，模拟调用 0 次。隔离的 5 个存储进程均停止，5 个临时 PostgreSQL 数据库均删除。C 盘可用空间从 23,673,507,840 B 变为 23,652,024,320 B。Docker 构建缓存仍为 22.67 MB，本轮 Docker 回收量为 0。其他长 I/O 和副本峰值继续按 RB-12、RB-14 原计划核对。[分类回执](../../tmp/intent-lifecycle-validation-20261121.json)
