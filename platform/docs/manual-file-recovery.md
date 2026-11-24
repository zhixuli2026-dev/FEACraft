# 文件保留版本的人工隔离与选择

## 2026-11-19 PostgreSQL、SeaweedFS 与独立 API 的恢复

使用临时 PostgreSQL、真实 SeaweedFS 和两个同时在线的独立 API 通过七组实际验收。两个 API 使用同一账户及确认键隔离同一版本，分别读到 `completed` 与 `pending`，标识相同。待处理请求最终读到同一份完成回执。旧目录快照的真实条件写入返回 HTTP 412，原存储版本和工作文件保持。

在实际分离待处理指针后、应用恢复文件但 SQL 尚未提交时、以及 SQL 提交之后分别退出原 API。替代 API 使用原确认键完成核对，目标文件与实际远端 `before/0` 或 `after/0` 一致。旧确认重放保留之后的新编辑。四类不同权限状态及撤权后的回执读取按原规则拒绝或允许。

四个前后相继的 API 与一个并发 API 均用创建句柄确认退出。独立核对 SeaweedFS 的原生身份已退出，临时 PostgreSQL 已删除。模型调用和模拟调用均为 0。本组使用同一台物理机器的独立 API，跨物理宿主的故障仍由 N7-01 原范围跟踪。[七组实际回执](../../tmp/manual-file-S3-2q_0tlfs/s3-result.json) · [对象服务退出](../../tmp/manual-file-S3-2q_0tlfs/store-exit-verified.json) · [临时库清理](../../tmp/terminal-agent-postgres-dl6v6dn1/cleanup.json)

专项入口为 [manual_file_recovery_s3_acceptance.py](../server/manual_file_recovery_s3_acceptance.py)，使用 `--postgres --review tmp/N7-01-manual-file-pg-s3-review-20261119.json`。启动时逐个校验本轮审核摘要及故障夹具。此入口在没有 `--postgres` 时沿用 SQLite。

2026-11-10，N5-07版本与隔离子阶段。本地文件SQLite/PostgreSQL、S3/SQLite及两端实际浏览器已有下述证据；未完成上传入口及原剩余范围继续，不能据本批关闭N5-07。主线38/53完成、剩15项，全部295项交付范围保持。

## 用户操作和含义

文件树上方新增“文件版本与人工恢复”。先读取当前待恢复事务或明确的准备版本，查看保留文件数量、原写者限制，填写处理原因。“登记保留原版本”仅写持久核查记录，不移动文件或放松写入限制。“确认隔离此版本”在原写者和存储占用检查后，FS无覆盖地移至私有隔离目录；S3生成独立、经摘要核对的保留副本，并对待处理指针执行下述隔离。原字节和原提交的不确定性继续保留，当前工作文件不被替换。

从已完成的隔离记录中选择保留文件，可预览最多前16 KiB。用户明确填写目标路径和恢复原因，先核对当前目标，再确认恢复。恢复是受当前目标摘要/字典版本约束的一次新编辑，不是改写原SQL提交或旧发布指针。原备份保留；caseDict沿用专用解析、阶段约束、字典镜像与文件事务，不经普通文件旁路。

确认在发送前写入绑定账户/算例的sessionStorage。响应丢失或并发处理中保留同一标识和原请求；刷新并重新选择原算例后可以继续同一次确认。后端在核对当前写权限后先读已提交结果，因此后来的用户编辑不会被旧确认重放覆盖。明确收到输入/目标冲突后，用户才能取消被拒绝的本机确认并重新核对。原现场、选择、说明及提交/收尾结果均有持久审计。

## API与约束

| 入口 | 用途 |
| --- | --- |
| `GET /cases/{id}/file-recovery?source=pending` | 当前待恢复版本的快照、文件、特殊条目与可用操作 |
| `GET /cases/{id}/file-recovery?source=preparing-<uuid>` | 明确准备版本的同类核对 |
| `GET /cases/{id}/file-recovery?source=objects/<uuid>` | S3明确对象版本的同类核对 |
| `GET /cases/{id}/file-recovery?source=uploads/<32hex>` | 不解析损坏上传元数据的原始暂存现场核对 |
| `GET /cases/{id}/file-recovery/actions` | 最近的持久人工记录 |
| `GET /cases/{id}/file-recovery/actions/{ticket}` | 原隔离记录与保留文件清单 |
| `GET /cases/{id}/file-recovery/actions/{ticket}/file?path=before/0` | 经保留摘要核验的文件读取，支持Range |
| `GET /cases/{id}/file-recovery/actions/{ticket}/restore-plan?path=before/0` | 所选文件的当前恢复方案 |
| `POST /cases/{id}/file-recovery/actions` | 固定Idempotency-Key的保留、隔离或显式恢复确认 |

上述路径均在`/api/v1`下。请求包括action、snapshot_sha256、expected_revision、expected_event_sequence和必填note。恢复另需isolation_id、file_path、target_path及expected_target_sha256；null明确要求目标不存在。来源版本变化或目标冲突时拒绝，不自动选旧内容或换提交标识。

审计使用现有DurableEvent/outbox的requested、committed、settled、rejected事件，不新增数据库表。确定性ticket和事件ID绑定账户、算例、提交键及完整请求摘要。现有CaseStorageOperation增加可选的初始日志绑定参数，在获取占用的同一次提交中绑定ticket/快照/请求事件，覆盖获取后尚未绑定的API退出空窗；旧调用不传参数时保持原行为。并发同键返回同一记录，仍在执行时可返回pending；不能把pending当完成。

隔离和恢复复用case控制锁、存储操作占用、会话停止及Job写入结束检查。活跃、未知或缺证明的原写者不得因人工登记而被当作已停止。进程退出后接续依赖原存储操作的实际出生身份观察，时间戳和租约过期不构成停止证明。隔离的物理目录移动与SQL完成事件之间退出时，新API核对原位置缺失及隔离目录的完整相同内容，再补同一审计，不重新移动或执行原Job。

本地目录清单最多512项、普通文件总量2 GiB，单文件沿用1 GiB上限。遍历不跟随符号链接、重解析点或硬链接；这些特殊条目只保留自身的元数据观察，不能被选择为普通文件恢复。恢复复制沿用文件事务、用户/节点峰值准入和私有路径保护。保留文件读取再次核对摘要和同一文件身份。部分JSON和缺少有效清单的前后镜像可以保留/隔离；不据目录名断言原提交状态。

## S3隔离与中断接续

S3按明确的pending或objects/<uuid>读取有界清单，逐对象记录ETag、长度和SHA-256；不以本地镜像作为远端事实源。保留副本使用不可覆盖的条件写入，读回核对后保存快照及隔离标记，原远端前后镜像不删除。损坏指针无法识别对象ID时，只保留该指针；用户须再明确选择其他对象版本，不能推断其关联。

移开pending前再次核对原始字节与条件身份，在既有存储占用下封闭旧目录版本，再条件删除逻辑指针。必须使用支持永久目录墓碑的现有S3 live-store客户端；不支持必要条件操作的后端拒绝该动作。真实旧HEAD条件PUT返回412已有证据，尚未宣称覆盖代理中在途请求的所有时序。已知活跃或无法核验的写者阻止隔离；无法识别的损坏指针可明确保留并隔离，但不会产生原写者停止证明。

恢复使用同一实际文件事务，并将原人工请求/来源摘要绑定其清单。API在实际应用后、SQL提交前退出时，原确认先核对并回滚未提交事务，再执行该明确的新编辑；SQL已提交时只补收尾与原回执，不再次写入。旧进程的远端额度通过原有所有者对账核验实际退出并结算，不按超时清零。SQLite先准入账户额度再写业务锁，避免反向占用。只读成员的方案不再列写操作，权限撤销后即使已有完成回执也拒绝访问；存储操作状态显示实际FS/S3后端。

## 实际证据

- `tmp/manual-file-recovery-_nqr7hdz/manual-result.json`：SQLite五组实际场景通过，包括损坏/部分清单保留、同键并发一次隔离且工作文件不变、明确选择与目标冲突、旧确认不覆盖后来的编辑、未知旧Job元数据拒绝隔离，以及真实API在目录移动后退出、新API同键完成一次审计。两次API出生身份由持有的创建句柄确认退出。未知Job只是明确的历史元数据故障，不是实际运行或模型执行。
- `tmp/manual-file-recovery-u0vzxju_/manual-result.json`：PostgreSQL同五组实际场景通过，两API创建句柄退出。`tmp/manual-file-postgres-zi8tzyv5/result.json`确认自建UUID数据库完成从空库的增量升级，并由独立连接确认已删除；开发数据库未改，不宣称完整混合旧数据回滚验收。
- `tmp/manual-files-browser-b6quch7a/UI-result.json`：真实浏览器完成登记、隔离、候选预览、目标变化409和重新确认。实际恢复提交后中断202响应，再保存新的目标内容；自然刷新并重新选择算例、沿用原key/body读取相同完整回执，新的目标内容保持。原前/后镜像仍可读取，只有一次隔离和一次恢复、没有业务Job。两个页面自动PTY均有持久停止证明，API和预览创建句柄确认退出。
- 真实模型调用0、模拟LLM调用0。这些是实际SQL、文件、HTTP和API退出验证，不能当模型推理证据。`tmp/N5-manual-files-UI-reviewed-runtime-v1-20261110.zip`保留941个审查源码/构建文件及审查登记，供核对当时实际运行的版本；后续修改不冒充原源码。

第一轮`tmp/manual-file-recovery-k1z2r2rr/manual-result.json`为失败并保持false：两个同键请求同时观察空闲时，后到请求返回file_sync_busy，未读回原确认。修复后，重新授权并匹配原ticket/请求/占用绑定，实际确认原API出生身份仍匹配后返回pending原回执；后续SQLite/PG通过。浏览器驱动曾在有内容的包装label精确定位和fetch Response.status属性上出错，实际页面/提交未因此重做，定位与属性错误保留为驱动问题。

桌面深色及浅色实际截图已查看，控件至少44px、键盘焦点2px、组件无横向溢出。390px下项目Dock默认仅99px，拖动后约159px，虽无组件横向溢出，完整移动可用性没有通过，继续由N6工作台布局/导航处理。构建和oxlint退出0，源码设计扫描为空；源码扫描不是完整渲染审查。

S3新增实际证据：

- `tmp/manual-file-S3-tn7rg3gk/s3-result.json`：7组实际S3/SQLite场景通过，包括损坏指针保留与真实旧条件写入412、独立前后镜像与新编辑、同键并发pending/completed最终同回执、外来/只读/成员/撤权拒绝、隔离后API退出接续，以及恢复提交前/后真实API退出后沿用原确认。4个API持有创建句柄确认退出，`store-exit-verified.json`独立确认自建对象存储原生出生身份已不存在。
- `tmp/manual-files-S3-browser-rerjgeyi/UI-result.json`：真实页面依次登记、隔离损坏指针、选择并隔离远端对象、预览before/0、明确提交新编辑；实际202提交后中断响应，自然刷新并重新选择算例，同key/body读取完全相同回执。后续新编辑仍保持，前后镜像与原远端副本均未变化，只有2次隔离、1次恢复、0个业务Job。两个页面PTY持久停止，API/预览创建句柄退出，自建S3原生退出由`store-exit-verified.json`确认。
- 浅/暗截图已实际查看；按实际计算样式及祖先背景合成测得文字对比度最低分别6.42/6.00，控件高度至少44px，键盘Tab焦点2px，无组件/页面横向溢出。没有重新宣称完整移动工作台可用。初始损坏指针造成正常读取409，隔离后正常读取恢复；缺省空算例的可选文献/几何404和既有THREE.Clock警告保留。
- S3后端及浏览器本阶段真实模型0、模拟LLM0；属于实际SQL/对象存储/HTTP、故障与界面验收。948个源码/构建成员的浏览器审查运行包为`tmp/N5-manual-files-S3-UI-reviewed-runtime-v1-20261110.zip`；7组后端的471个源码成员为`tmp/N5-manual-files-S3-reviewed-runtime-v5-20261110.zip`，均可逐文件与运行前审查摘要匹配。

S3前三轮失败保持false：`m8_ijzsr`、`ph7d68p7`第一次恢复因SQLite在业务写锁之后才申请额度被拒绝；修复准入顺序后，`f2zo9ajr`在真实API退出的旧远端额度尚未结算时拒绝接续。随后接入既有所有者对账，`ittrfewx`6组及`tn7rg3gk`新增并发后的7组通过。没有用新成功覆盖旧失败，所有这些轮次的自有API和对象存储退出均分别保留证据。

## 继续交付

未完成上传已有服务端发现、核对原文件后接续、取消清理和登记保留；损坏/缺失元数据另可转入[原始上传现场隔离](damaged-upload-isolation.md)。[历史分页与跨浏览器原确认接续](manual-confirmation-continuation.md)已完成FS/SQLite和真实浏览器验收；FS/S3的caseDict、来源变更、上传分页及API退出后原确认接续也已通过，见[原条件核对](N5-07-original-closure-audit.md)。原停止限制及全部N6/N7完整部署范围保持，不能将这些结果扩大为全部PostgreSQL/跨主机矩阵。

## 2026-11-19 大文件恢复方案与并发保存

公开 `restore-plan` 查询原先在持有算例控制锁时核验所选隔离文件的完整摘要。SQLite 上的原版真实对照暂停在 40 MiB 读取入口，独立连接无法取得写锁，同算例普通文件保存返回 HTTP 409，事件顺序保持 7。恢复读取后方案返回原文件长度和 SHA-256，隔离 API 已核验退出。[基线回执](../../tmp/manual-restore-plan-ft_8mdal/manual-restore-plan.json) · [执行器摘要](../../tmp/vibeflow-regression-all-20261119-150415-654c84/summary.json)

现在先授权并读取已完成隔离的持久记录，核验大文件前释放数据库连接。核验后再次授权，检查同一存储绑定和隔离记录，再在短控制事务内读取当前修订、事件顺序及恢复门禁。确认提交继续复用既有明确目标、原内容检查及幂等回执。[恢复方案实现](../server/app/services/manual_file_recovery.py:315)

SQLite 与本地文件系统复验 1/1，耗时 49.406 秒。核验入口暂停时，独立连接取得写锁，同算例保存先返回 200，方案使用事件顺序 9。实际恢复 41,943,040 B，公开下载 SHA-256 为 `c24fd321f34ab8e4d48b8b91fa22d2243bb0d821d4dd6949f15740f147b9c7e8`。同一确认键返回同一回执。只读读者可以查看，核验期间撤权使本次及后续只读请求均返回 404。一字节篡改保留文件返回 409，已恢复的目标仍与原件一致。隔离 API 出生身份已核验退出。[实际文件系统回执](../../tmp/manual-restore-plan-u20m8tnl/manual-restore-plan.json) · [执行器摘要](../../tmp/vibeflow-regression-all-20261119-150643-83be55/summary.json)

SQLite 与认证 SeaweedFS 4.52 复验 1/1，耗时 148.828 秒。独立写锁和同算例并发保存均通过，方案使用事件顺序 9。已隔离、恢复和公开下载 41,943,040 B，SHA-256 与文件系统相同。固定确认键、历史只读权限、核验中撤权 404、同长度坏对象 409 及已恢复原件保持均通过。隔离 API、SeaweedFS 启动器和原 Linux 出生身份已核验退出，原隔离目录无活动成员。[实际 S3 回执](../../tmp/manual-restore-plan-39g7ycic/manual-restore-plan.json) · [执行器摘要](../../tmp/vibeflow-regression-all-20261119-150802-ad96f6/summary.json) · [SeaweedFS 退出](../../tmp/manual-restore-plan-39g7ycic/s3-process-exited.json)

两个实际算例的写入操作均已结清。夹具在最后一次 `raw` 回读完成后立即终止 API，两个隔离根各留下一个 41,943,040 B 的节点预留记录，记录内的 API 出生身份均已核验退出，暂存根没有对应普通文件。本次实测覆盖公开恢复方案的锁、源绑定和权限，以及实际恢复后的公开回读。原阶段的其他入口继续沿用现有清单。C 盘可用空间 22.156→22.159 GiB，D 盘 390.491→389.881 GiB，夹具均在 D 盘，模型调用和 Docker 写操作均为 0 次。[本地预留观察](../../tmp/manual-restore-plan-u20m8tnl/post-exit-resource-observation.json) · [S3 预留观察](../../tmp/manual-restore-plan-39g7ycic/post-exit-resource-observation.json) · [原阶段清单](N4-05-remaining-evidence-audit.md)

### RAW 下载退出后的旧预留复验

用原隔离根实际回收两条预留。原 SQLite 通过只读连接复制，新 API 使用数据库副本。SeaweedFS 使用原端点绑定的实际数据副本，比较原对象存储文件哈希保持。节点记录的绝对路径和进程出生身份按原字节保留在独立证据备份，实际回收在其绑定的隔离根执行。回收前后两套原 SQLite 和其他本地文件摘要均保持。[实际回放入口](../server/raw_download_claim_recovery_smoke.py)

文件系统的精确复演和认证 SeaweedFS 的原记录均在健康检查、后台账户首轮及公开维护 HTTP 200 后仍存在。下一次正常 `/raw` 准入经 `SpooledDownload.reserve_body` 和节点容量扫描，核对原进程确已退出并回收记录。两个公开请求都返回 HTTP 200、原 41,943,040 B 和相同 SHA-256，新下载结束后节点预留归零。文件系统执行器 1/1，25.547 秒。SeaweedFS 执行器 1/1，25.938 秒。这个结果证明下一次正常节点准入可以回收已确认退出的普通预留。启动时点取决于当时是否有需要节点准入的操作。[文件系统复验](../../tmp/raw-download-claim-recovery-nqy87adb/raw-download-claim-recovery.json) · [文件系统执行器](../../tmp/vibeflow-regression-all-20261119-152114-8b98a5/summary.json) · [SeaweedFS 复验](../../tmp/raw-download-claim-recovery-m5j1nixk/raw-download-claim-recovery.json) · [SeaweedFS 执行器](../../tmp/vibeflow-regression-all-20261119-152518-7c428c/summary.json) · [节点扫描实现](../server/app/services/node_storage_budget.py:69)

首轮文件系统在健康检查时已经回收记录，夹具原先断言旧记录仍存在并失败。两次实测前的完整树摘要只差一份已提交的创建记录从 `pending` 进入 `history`，该生产落盘路径也调用原节点准入。首轮缺少实时调用栈，这项盘面关联保留为解释，后续精确复演捕获了 RAW 准入调用栈。首轮 SeaweedFS 副本未在既有就绪期内启动，原节点记录和原对象存储摘要保持，启动进程已核验退出。[文件系统首轮](../../tmp/raw-download-claim-recovery-ccvjlsvf/raw-download-claim-recovery.json) · [首轮创建回执旁证](../../tmp/raw-download-claim-recovery-ccvjlsvf/early-claim-provision-correlation.json) · [SeaweedFS 首轮](../../tmp/raw-download-claim-recovery-rat7pdrb/raw-download-claim-recovery.json) · [失败对象服务退出](../../tmp/raw-download-claim-recovery-rat7pdrb/s3-process-exited.json)

隔离 API 33784、32044 及 SeaweedFS 的 Linux 出生身份已核验退出。回收的是两笔逻辑预留，原 API 退出后对应暂存文件已经消失。C 盘可用空间 22.150→22.137 GiB，D 盘 389.881→389.365 GiB。模型调用和 Docker 写操作均为 0 次。[通过对象服务退出](../../tmp/raw-download-claim-recovery-m5j1nixk/s3-process-exited.json)
