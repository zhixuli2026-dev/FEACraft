# 分块上传预留转移与永久关闭标记

## 2026-11-20 S3 分片正文写入

普通分片请求以持久算例租约协调取消、完成和同偏移竞争。正文、描述记录、进度元数据及已发送请求的收尾在 SQL 写事务外。短事务核对算例绑定和人工选定的续传来源，登记额度并提交最终偏移。分片释放原上传承诺中的正文额度给本次写入，额外空间只覆盖描述记录、元数据及写入日志。额度回执标记有界池，历史无标记的池按原排他规则核对。

| 验收 | 实际结果 | 证据 |
| --- | --- | --- |
| 修复前，SQLite 与认证 SeaweedFS | 暂停 4 MiB 正文 PUT 时，独立事务无法取得 SQL 写锁，同账户另案保存需等原上传恢复。分片的活动账户池为 2,147,464,783 B。 | [基线](../../tmp/live-cli-startup-o_ylp6u8/upload-chunk-write-lock.json) |
| 修复后，SQLite 与 PostgreSQL | 相同暂停点可取得 SQL 写锁，同账户的另案保存及另一上传均完成。原上传同偏移竞争返回 409。4 MiB 分片的账户池为 4,326,416 B，结清后为零。 | [SQLite](../../tmp/live-cli-startup-_7xrpegp/upload-chunk-write-lock.json) · [PostgreSQL](../../tmp/live-cli-startup-0eoni6bj/upload-chunk-write-lock.json) |
| 紧贴额度上限 | 两种数据库均以 9,437,184 B 的测试账户上限验证。额外留出 150,000 B，实测远端用量 9,288,408 B，正文从原上传额度转移且最终结清。 | [SQLite](../../tmp/live-cli-startup-_7xrpegp/upload-chunk-write-lock.json) · [PostgreSQL](../../tmp/live-cli-startup-0eoni6bj/upload-chunk-write-lock.json) |
| API 退出和同偏移重试 | 两种数据库的分片及取消组合各有 5 次实际 API 退出。1 MiB 正文送达后退出，提交不同字节返回 409，原字节重试复用原 ETag，额外正文转移为 0，最终内容比对一致。 | [SQLite](../../tmp/live-cli-startup-z0eso3xn/upload-cleanup.json) · [PostgreSQL](../../tmp/live-cli-startup-665qnrne/upload-cleanup.json) |
| 期限与排空 | 独立 SQLite + 认证 S3 把本次请求期限设为 15 秒。已开始的 1 MiB PUT 在期限结束后排空，接口返回 504。描述记录尚未创建，元数据偏移保持 0，随后原请求复用已有 ETag 并结清。 | [期限实测](../../tmp/live-cli-startup-08ua2b5s/upload-chunk-timeout.json) |
| 文件系统兼容 | 文件系统完成和取消的四组真实退出通过。 | [本地回归](../../tmp/live-cli-startup-2maurmou/upload-cleanup.json) |
| Chromium 兼容 | 使用工作区已有的上传修复构建，验证真实 S3 的创建和首块响应丢失后恢复、同上传 ID 的 5 个偏移、总计 17,825,805 B、发布摘要与多段 Range。首轮在分片发送前的创建阶段返回 503，随后单独复验通过。 | [正式验收](../../tmp/live-cli-startup-g06k96yc/upload-creation.json) · [首轮失败](../../tmp/live-cli-startup-yobv7qjk/browser-failure.json) |

本阶段使用真实 `deepseek-flash` 回读隔离验收结果，基线 2 次、首轮并发复验 4 次、最终复验 14 次，共 20 次 HTTP 200，模拟 0 次。期限脚本首轮因测试端缺少 Python `time` 导入停止，修正后按真实 S3 运行。[浏览器后的模型回读](../../tmp/live-cli-startup-g06k96yc/live-result.json) · [首轮期限脚本记录](../../tmp/live-cli-startup-zlqzu651/live-result.json)

验收前后 C 盘可用空间为 22.02 GiB 和 22.03 GiB，Docker 构建缓存为 22.67 MB。Docker 内部回收和 Windows 的对应实际释放均为 0。定时清理最近一次运行成功。

## 2026-11-18 上传清理被中断后的恢复

本地文件上传在清理前先核对会话 ID、目录内容及额度绑定。已提交的上传还要核对完成回执中的元数据摘要、文件长度与 SHA-256。随后在同一卷上把目录改名为 `.cancelling` 或 `.completing`，逐个删除已核对的普通文件，最后删除元数据和空目录。旧版无额度记录的会话在移走文件前先持久化其归属及实际剩余字节。进程中断后，使用同一上传 ID 重试即可继续。发现新文件时保留现场，已发布文件仍可读取。

S3 的一次分块写入会创建正文和描述记录两个对象。取消现在覆盖每个已登记偏移的两个键，包括中断时尚未出现的键。重试可核对并补清旧版本已经结算、但分块仍在的上传。如果 SQL 显示已取消而存储显示正在上传，返回 409 并保留现场。账户额度对账核对原条件写入的对象键、旧 ETag、精确字节摘要以及算例和上传绑定。只有已知取消元数据可以凭这条证明结算。其他可变写入仍按各自的证明处理。

| 组合 | 实际故障与结果 |
| --- | --- |
| 本地文件 × SQLite、PostgreSQL | 分别在删除数据和元数据后结束 API 进程。每种数据库验证取消与已发布文件清理共四次退出，覆盖无旧额度记录的会话。取消在重试后释放剩余额度，重复完成返回同一回执。往退役目录加入无关文件会阻止清理，该文件和相邻目标都得到保留。 |
| SeaweedFS × SQLite | 第二块正文发送前退出。重启后在已写入取消元数据、尚未收到应答的位置再次退出。目标元数据多一个空格时原请求的额度仍被保留。恢复原字节后可结算，重试取消后账户写入预留全部为零。 |
| SeaweedFS × PostgreSQL | 同样的两次退出，第二块的中断点改在正文已到达、描述记录尚未发送时。重试后，两个偏移的四个分块键都拒绝迟到写入，旧元数据的条件写入也返回 412。两种 S3 组合均复验了旧版提前结算记录。 |

四组最终验收共观察到 12 次 API 进程按指定位置退出。每组随后由真实 DeepSeek 调用读文件工具并回读结果摘要，共 8 次上游 HTTP 200。模型回读和故障注入分别记账。首轮本地 SQLite 暴露了 Windows 共享句柄短时阻止删除空目录，现复用有界重试。两次早期 S3 运行在创建隔离算例时超时，保留失败记录；最终分开启动两种 S3 组合，并对回环地址使用直连。

[本地 SQLite](../../tmp/live-cli-startup-e19kxnno/upload-cleanup.json) · [本地 PostgreSQL](../../tmp/live-cli-startup-1lzrlt4x/upload-cleanup.json) · [S3 SQLite](../../tmp/live-cli-startup-2izob8et/upload-cleanup.json) · [S3 PostgreSQL](../../tmp/live-cli-startup-muftn071/upload-cleanup.json) · [实际验收入口](../server/live_llm_upload_cleanup_acceptance.py)

在服务器虚拟环境执行 `python -B live_llm_upload_cleanup_acceptance.py --source-profile-id 26`。添加 `--postgres` 或 `--s3` 选择对应的隔离基础设施。验收使用临时数据库、算例及独立端口。FF-H17 的上传创建响应丢失、尚无会话 ID 时的预留和浏览器部署复验继续跟踪。RB-12 其余暂存和导出入口仍在资源清单中。

2026-11-06：S3 分块写入新增持久额度转移，把已经预留、尚未物化的内容空间移入本次所有者写入额度。新迁移为 `e2c7e93068af`，增加 `upload_chunk_credits`，记录对应写入额度、上传 ID、存储绑定、偏移、长度、摘要、转移字节及结算凭据。

## 额度与恢复协议

2026-11-06 的原实现是在所有者额度锁和算例执行锁下核对远端进度、现有分块、未来预留和实际用量。2026-11-20 的锁外盘点更新见后文。只有不存在的内容才转移对应字节；相同不可变分块已存在时，核验后复用，不申请第二份内容空间。SQL 在同一事务中减少未来预留、创建所有者额度和转移记录。同一上传最多有一份活动转移。

写入范围只允许固定键、长度和摘要的条件创建消耗转入的内容额度，其他元数据与恢复日志继续使用额外余量，关闭日志空间也受保护。完成真实分块及进度写入后，再提交上传预留和结算写入额度。异常或退出恢复会读取绑定的远端状态，已物化但未推进元数据的内容不再作为未上传字节重复预留；无法核验的状态保留整个额度，未知请求保留其字节，不按 TTL 释放。

第一次故障验证发现，普通上传分块原本在清理时删除，不能直接使用永久不可变对象的恢复证明。现在完成和取消把分块键条件替换为零字节关闭标记；键始终存在，迟到的 `IfNoneMatch=*` 写入会被拒绝。取消还会根据转移记录补齐尚未出现在目录列表中的分块关闭标记。已完成文件和持久完成凭据不受此改变影响，历史版本仍按实际用量计费。

零字节标记是恢复协议的一部分，不能按普通暂存文件清除。未来维护、回收、迁移工具必须保留这条约束，不能因内容大小为零就删除其键。跨版本部署需要停止旧写入进程，防止旧清理代码继续删除这些键。

## 实际验收

两个显式入口均未调用 LLM 或模拟推理：`upload_chunk_credit_checks.py` 验证额度与故障，`upload_chunk_fence_checks.py` 验证关闭标记。实际 S3 使用独立 SeaweedFS；两种数据库均通过 Alembic 初始化隔离库后测试。

| 场景 | 核验内容 |
| --- | --- |
| 小额度余量 | 1 MiB 限额下，较大的已预留内容可以写入；物理用量仍不超过限额 |
| 元数据写入前、写入后失败 | 根据实际远端进度恢复，已写内容复用；提交后的原偏移重试拒绝 |
| 内容请求发送前、实际写入后丢失响应 | 先保留未知请求额度；有足够余量及可验证证据时恢复，不把未知结果算成成功 |
| API 在内容 PUT 后退出 | 实际进程以 79 退出，活动转移和额度仍存在；新进程凭原数据库和远端证据继续上传 |
| 内容写入次数 | 六个分块各发生一次实际内容 PUT；已存在内容的重试没有第二次物化 |
| 完成及取消 | 同键保留零字节标记，实际迟到的条件 PUT 返回 412，额度结算 |
| 尚未物化的分块取消 | 为已记录但目录中尚不存在的键建立关闭标记，迟到写入仍被拒绝 |
| 数据库迁移 | SQLite、PostgreSQL 实际升级；有转移历史时拒绝降级丢弃证据 |

额度/故障组每库 12 条、关闭标记组每库 4 条，合计 32 条检查记录。证据：[SQLite 额度与故障](../../tmp/upload-chunk-credit-vm3n2v25/checks.json)、[PostgreSQL 额度与故障](../../tmp/upload-chunk-credit-wplf0f8n/checks.json)、[SQLite 关闭标记](../../tmp/upload-chunk-fence-fhn13g6k/checks.json)、[PostgreSQL 关闭标记](../../tmp/upload-chunk-fence-iunqb_ug/checks.json)。新增转移后还复验了 [11 条上传写入及共享成员场景](../../tmp/upload-write-capacity-jt77adai/checks.json)；该兼容组早于关闭标记修正，关闭标记以专门测试为准。

失败记录未覆盖：[旧恢复规则未识别上传分块](../../tmp/upload-chunk-credit-wdoouxuv/checks.json)、[观察器误将零字节关闭写入当作内容](../../tmp/upload-chunk-credit-t7lscsbv/checks.json)、[SQLite 测试库缺少迁移版本初始化](../../tmp/upload-chunk-credit-fnjr93b7/checks.json)、[PostgreSQL 测试记录未排序](../../tmp/upload-chunk-credit-ayzyzurd/checks.json)。另修正了新迁移检查约束中 PostgreSQL 保留字 `offset` 的引用；[失败迁移日志](../../tmp/mixedread-postgres-alu2tq41/alembic.log)保留。

## 部署与剩余范围

仅迁移了隔离测试库，没有修改开发数据库或重启开发服务。部署代码前需要在备份和停止旧写入进程后执行新迁移；应用会拒绝用旧数据库结构启动。带转移记录的数据库不能通过降级直接删除历史。

本批没有证明所有并发和故障组合均已完成：FS 完整矩阵、跨 worker 同时写入/取消、长时间阻塞请求、未知可变元数据请求、接近额度耗尽时仍未获得证明的旧请求，以及旧共享操作者预留的归属修复继续跟踪。上传请求体的流式内存上限、对象数和历史扫描预算还属于后续资源工作。

RB-12 仍需其他直接文件写入和 ZIP/备份/导出峰值，RB-13 至 RB-17 及全部 295 项交付保持。主线仍完成 33/53、剩余 20 项；资源预算完成 11/17、剩余 6 块。32 条检查不计为新增完成任务，真实模型调用累计数不因本批基础设施验证增加。

## 2026-11-20 分块额度盘点与期限

分块申请先读取绑定的远端元数据与已存在分块的摘要，再盘点账户实际用量。盘点后复核两个对象的版本，SQL 写事务内复核账户版本、当前额度、归属和上传状态。转移和写入额度仍在同一个事务提交，老会话首次登记仍需核对剩余负担。只有在盘点前已经存在、且字节一致的分块才可以复用；潜在的迟到请求继续按原保留记录计费。实际写入保留条件操作及上传偏移核对。

- 基线在盘点 21 条、9,795 B 后暂停，独立 SQLite 写事务不可开。修复后盘点 39 条、18,859 B 时，另一账户修改和同账户另案保存成功。版本 8 对比 48 后重扫 47 条、22,135 B，当前请求没有额外的锁内盘点。126,976 B 分块完整发布并读回，转移及上传预留结清。[基线](../../tmp/storage-chunk-inventory-w6axqhkj/storage-admission.json) · [修复](../../tmp/storage-chunk-inventory-v0h2bysu/storage-admission.json)
- 暂停真实盘点直到请求预算用尽。活跃线程排空前接口不返回，SQLite 写连接仍可用；排空后接口返回 503，前后均未新增账户预留和分块转移，上传偏移仍为 0。同一请求重试后读回 94,208 B。[期限与排空](../../tmp/storage-chunk-deadline-32_rl7p_/storage-admission.json)
- 两库和认证 SeaweedFS 的额度矩阵各 12 项通过，含真实 1 MiB 限额、元数据更新前后故障、正文发送前后丢失响应，以及实际 API 进程退出。每库六个分块，各只有一次成功的物理正文写入；未知请求保留额度，已物化分块重试不重复写入；历史转移拒绝被降级丢弃。[SQLite](../../tmp/upload-chunk-credit-7gqureef/checks.json) · [PostgreSQL](../../tmp/upload-chunk-credit-4d98zcu9/checks.json)
- PostgreSQL + 认证 SeaweedFS 的取消专项额外验证两次 API 实际退出和 5 次迟到条件写返回 412，旧结算记录仍能恢复，全部额度结清。真实 DeepSeek `deepseek-flash` 两次 HTTP 200，工具读回准确；模拟 0。其余并发、额度和超时专项没有模型调用。[取消](../../tmp/live-cli-startup-2doecvqx/upload-cleanup.json) · [真实模型](../../tmp/live-cli-startup-2doecvqx/live-result.json)

- 两库的永久关闭标记专项各 4 项通过：完成、取消及未到达正文的取消都会保留有条件的关闭记录，实际迟到 PUT 返回 412，账户额度结清。两轮均使用认证 SeaweedFS，PostgreSQL 隔离库已删除。[SQLite](../../tmp/upload-chunk-fence-n1lyz9zu/checks.json) · [PostgreSQL](../../tmp/upload-chunk-fence-rq450ogg/checks.json)

本批无新增迁移或开发库改动。旧的未知额度回收仍可能在写事务中读取远端证明，实际安装和发布仍需协调写入。RB-14 后续按原范围验证这些慢存储路径、取消与心跳的一体组合。
