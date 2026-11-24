# 导入、复制与备份恢复的提交补偿

## 2026-11-21 跨卷准备与安装

算例 ZIP 导入、算例复制、含算例的工作区复制及 v1 工作区备份恢复按目标算例目录所在卷选择暂存。分别挂载的算例目录复用目标卷上的私有暂存，按该卷可用空间申请节点预留。在 SQL 写事务外写入并校验正文，事务内核对文件版本后原子安装目录。用户容量检查扣除本次已记入同一用户用量的暂存正文，继续计入新增字典等元数据。工作区和全部成员共用一次提交。

WSL 实测以 ext4 为存储根、tmpfs 为算例目录。四个入口各处理 12 MiB 正文，工作区复制处理两名成员。每次校验完正文后暂停，独立 SQLite 写事务及另一账户的保存均成功。公开接口读回原 SHA-256，安装前后的设备和 inode 一致。依次在 13、25、49、61 MiB 配额下成功，再次导入超额返回 429 且无新增算例。另在安装后、提交见证前终止 API，实际重启并经恢复接口回滚，同名重试读回相同摘要。暂存与节点预留清空。[双卷验收](../../tmp/cross-volume-constructor-20261121.json)

Windows 文件系统与 SQLite 通过 18 项归档回归，实际覆盖解压取消、额度拒绝和二进制备份恢复。StandardCase 40 MiB 导入在 Windows 文件系统与 SQLite、认证 SeaweedFS 与 PostgreSQL 上分别通过，修改暂存返回 409，随后同名重试成功。临时 PostgreSQL 库已删除。本轮真实模型和模拟均为 0。跨卷验收使用 Linux 挂载。[归档回归](../../tmp/archive-capacity-tz1wyvu3/checks.json) · [Windows 导入](../../tmp/standard-case-stream-import-_3l476dm/standard-case-stream-import.json) · [认证 S3 导入](../../tmp/standard-case-stream-import-fi75z036/standard-case-stream-import.json) · [临时库清理](../../tmp/terminal-agent-postgres-ggtzmkao/cleanup.json)

## 2026-11-21 冷启动构造回滚

主动恢复、创建前恢复及后台分页维护共用构造待决记录。恢复先读取清单，并在写事务外按块核验正文。随后在账户锁内重读同一记录和 SQL 提交凭证，核对当前文件清单与版本后才回滚。文件系统使用含 NTFS 变更时间的身份复核，SeaweedFS 按原 ETag 条件删除。已提交的记录补写历史回执，未提交工作区的日志改名仍支持更新前和更新后的两种布局。分页游标按原版本推进。[回滚实现](../server/app/services/case_provision.py) · [后台维护](../server/app/services/storage_maintenance.py)

| 文件存储 | SQLite | PostgreSQL |
| --- | --- | --- |
| Windows 文件系统 | [主动恢复](../../tmp/workspace-restore-install-_ivu7d9x/restore-install.json) | [分页维护](../../tmp/workspace-restore-install-dfma6q2j/restore-install.json) |
| 认证 SeaweedFS | [主动恢复](../../tmp/workspace-restore-install-yqu5_mk9/restore-install.json) | [分页维护](../../tmp/workspace-restore-install-fjo0wgvd/restore-install.json) |

四组均在实际 API 退出后暂停读取 12 MiB 待回滚正文，另一算例仍保存成功。文件系统使用同账户，SeaweedFS 使用其他账户并沿用账户容量凭证。Windows 等长并保留修改时间的改写被识别，改动和记录均保留。期限耗尽后排空读取，原入口返回 409，可复验后重试。四组均完成按原包重试及日志读回。旧版在同一 12 MiB 观察点阻塞独立 SQLite 写入。[旧版基线](../../tmp/workspace-restore-install-7wr9btd3/restore-install.json)

另以 12 MiB 真实构造文件在 `os.link` 安装后实际退出，核验同一文件的两个链接。后台维护校验临时文件期间同账户仍可保存，随后清理暂存名称、回滚未提交文件并按原名创建。八组原生硬链接退出及十组旧记录兼容通过。真实模型与模拟均为 0。[硬链接并发回执](../../tmp/provision-linked-stage-lock-vxyi5t77/stage-lock.json) · [退出兼容](../../tmp/provision-stage-evidence-s5fl3q3r/checks.json) · [旧记录兼容](../../tmp/stage-compat-evidence-71_g6fe9/checks.json)

## 2026-11-20 复制并发与超时排空

公开单算例和工作区复制共用每 API 进程 4 个名额与 4 个独立的 I/O 线程。请求通过权限检查后尝试取得名额，满额立即返回 `429 case_copy_busy` 和 `Retry-After: 1`。请求原期限覆盖已有的事务和存储操作。期限到达时，已派发的线程继续排空，资源结清后释放名额，后续写入仍按原期限检查。复制请求中的 S3 容量凭证和截止时间跟随工作线程。[实现](../server/app/services/case_copy_io.py)

旧版有 5 个不同线程同时读真实文件，5 个请求随后同时交付时有 4 个因 SQLite 写入竞争返回 409，此次对照整体判失败。新版在 SQLite 搭配文件系统和认证 SeaweedFS 的两次独立验收中，每次用 5 个账户各保留 40 MiB 源文件，两个接口交错占满 4 个线程，SQLite 仍能开启写事务。第 5 个请求在满载以及超时线程仍未排空时均返回 429。放行后旧工作区请求返回 `504 workspace_copy_timeout`，公开列表无目标，第 5 个请求重新提交并读回同 SHA-256，其他 3 个请求随后依次成功，节点预留归 0。真实模型 0，模拟 0。[旧版对照](../../tmp/copy-workers-fs-0pxevlwe/workers.json) · [文件系统](../../tmp/copy-workers-fs-aaqjbwlg/workers.json) · [认证 S3](../../tmp/copy-workers-s3-ku_hrw3q/workers.json) · [套件审查](../../tmp/case-copy-admission-review-20261120.json)

文件系统的七项最终复制用例和认证 S3 的六项复制用例已复跑。真实 API 退出与未提交对象回滚、同名重试、源版本变更及复制件实际运行 Windows 作业均通过。独立核验九个 API 进程、五个监听端口和两个临时 SeaweedFS 进程的退出。[文件系统回归](../../tmp/case-copy-final-fs-a1b8w3wb/final-copy.json) · [认证 S3 回归](../../tmp/case-copy-final-s3-vmm_8gal/final-copy.json) · [退出核验](../../tmp/copy-workers-fs-aaqjbwlg/exit-proof.json)

## 2026-11-20 S3 复制的最终准备与提交

公开单算例和含算例的工作区复制沿用 S3 账户容量凭证。来源扫描之后，以本次复制的 UUID 建立私有目标，先持久保存待决清单，再按块复制正文。在这一阶段完成目标内容和来源的 SHA-256 校验，释放数据库写事务。算例 ID 在最终事务分配。事务内核对来源与目标对象版本，用条件写入把待决清单绑定到实际算例和工作区，然后写入生成的字典与历史并提交见证。工作区和全部成员共用一次数据库提交。作业监督按持久记录中的算例 ID 登记，私有对象路径可独立于该 ID。[远端准备](../server/app/services/case_copy_remote.py) · [构造见证](../server/app/services/case_provision.py)

SQLite 与认证 SeaweedFS 使用 40 MiB 实际文件。正文暂停时 SQLite 可取得写事务，另一账户保存返回 200，同账户按原容量凭证返回 `409 storage_quota_busy`。复制件逐字节读回，Windows 作业真实运行并经退出核验。工作区复制遇到已确认的 S3 源变更返回 409，公开列表无部分工作区，同名重试读回两名成员与新文件。[六项实际验收](../../tmp/case-copy-final-s3-7t2tl6vk/final-copy.json)

在编号绑定前和写入提交见证前分别使实际 API 以 85、86 退出。冷启动从原存储读到两种待决清单，正式恢复入口清理未公开副本，同名重试读回完整 40 MiB 的相同摘要。额外将请求限额设为 30 秒，在正文读取暂停期间使其到期。请求待实际读取排空后返回 `504 case_copy_timeout`，待决清单可回滚、同名重试通过。两批节点预留最终均为 0。版本历史按现有维护策略保留与回收。模型调用 0，模拟 0。[超时与排空验收](../../tmp/case-copy-final-s3-097cuvzs/final-copy.json) · [执行前审查](../../tmp/case-copy-s3-final-review-20261120-v2.json)

本轮复跑文件系统最终复制七项和单算例首扫在文件系统、S3 上的兼容测试。独立核验九个 API 进程、五个 API 监听端口及三个临时 SeaweedFS 进程的退出。新增 S3 最终复制与冷恢复矩阵使用 SQLite。[文件系统复验](../../tmp/case-copy-final-fs-iy3z_pw8/final-copy.json) · [单算例 S3 首扫](../../tmp/case-copy-source-s3-i6jfzova/case-copy-source-lock.json) · [进程和端口核验](../../tmp/case-copy-final-s3-7t2tl6vk/exit-proof.json)

## 2026-11-20 文件系统复制的最终准备与安装

公开单算例和含算例的工作区复制先在短事务记录来源与权限。随后使用节点预留，在配置的文件存储根下准备私有目录。普通文件按块复制与摘要校验，源文件最终重读也在源写锁和账户事务之外。提交时再次核对源成员、文件版本、权限和额度，按已分配的算例 ID 生成字典事件和修订历史。原本的构造待决清单写入后，无覆盖地原子安装目录，再提交见证。各工作区算例仍共享一次 SQL 提交。[本地准备与安装](../server/app/services/case_copy_local.py) · [构造见证](../server/app/services/case_provision.py)

两个公开复制入口共享 `VIBEFLOW_CASE_COPY_TIMEOUT_SECONDS`，默认 300 秒。普通超时需排空已启动读取线程，删除本次私有目录并结清节点预留。各私有批次记录实际进程出生身份，后续复制可回收已证明退出的批次。原子安装后失去 API 所有者，交给原 `case-provisions:recover` 核对待决清单与 SQL 见证。单卷文件系统启用这一安装方式，目标在另一卷时沿用原有构造流程。

SQLite 与 Windows 文件系统的真实 40 MiB 对照中，旧版复制期间写事务无法开始，同案保存 409。新版同案保存 200，暂存的旧复制返回 409。复制工作区期间可新增源成员，旧请求拒绝发布，按原名称重试后读回包括新成员在内的三个算例。原件与复制件 SHA-256 一致。[旧版对照](../../tmp/case-copy-final-fs-44gcb6fd/final-copy.json) · [新版对照](../../tmp/case-copy-final-fs-7sjuiriy/final-copy.json)

最终版本通过七项实际检查。四秒限额在大文件读取期间触发，已启动读取排空后返回 `504 case_copy_timeout`，公开列表无目标。实际 API 在私有目录写入部分文件后以退出码 85 退出，重试清理已核验来源的残留。原子安装后、SQL 见证前以退出码 86 退出，冷启动通过正式恢复入口回滚，再按原名复制并读回相同摘要。节点预留和本轮私有批次均清零。模型调用 0，模拟 0。[七项验收](../../tmp/case-copy-final-fs-7lptx0kq/final-copy.json) · [源码审查](../../tmp/case-copy-final-stage-review-20261120-v2.json)

公开算例和工作区复制的首扫兼容在本地文件系统和认证 SeaweedFS 均通过。独立核验九个 API 进程身份和两个 SeaweedFS 进程的退出及端口关闭。[算例 S3 首扫](../../tmp/case-copy-source-s3-meix_fsi/case-copy-source-lock.json) · [工作区 S3 首扫](../../tmp/workspace-copy-source-s3-q280uku3/workspace-copy-source-lock.json) · [退出核验](../../tmp/case-copy-final-fs-7lptx0kq/exit-proof.json)

S3 算例和工作区构造的最终对象写入与提交见本页首节。S3 工况派生的事务改进另有完整回执。[S3 工况派生](direct-write-and-derive-capacity.md#2026-11-20-s3-最终复制与回滚)

## 2026-11-19 算例与工作区的大文件流式复制

算例复制和工作区复制将普通文件按块读入原有构造事务。文件系统直接写入已取得配额的暂存 `.part` 文件，完成校验后走原安装路径。S3 按来源版本读取，以节点额度保护本地暂存，再走原有条件写入和账户额度。来源大小与 SHA-256 保持可核验，业务来源锁继续维持到原提交边界。阻塞文件读取在工作线程执行。字典和历史等需要转换的内容继续走原有处理。[来源快照](../server/app/services/case_source.py) · [复制入口](../server/app/services/case_admin.py) · [构造事务](../server/app/services/case_provision.py)

| 部署 | 48 MiB 复制的 Python 峰值分配 | 复制耗时 | 实测回执 |
| --- | ---: | ---: | --- |
| SQLite 与文件系统 | 2.81 MiB | 5.672 秒 | [回执](../../tmp/case-clone-streaming-waefaa4w/clone-streaming-result.json) |
| PostgreSQL 与文件系统 | 2.66 MiB | 5.188 秒 | [回执](../../tmp/case-clone-streaming-tgac79my/clone-streaming-result.json) |
| SQLite 与 S3 | 3.33 MiB | 17.985 秒 | [回执](../../tmp/case-clone-streaming-1jf7ug3g/clone-streaming-result.json) |
| PostgreSQL 与 S3 | 3.52 MiB | 18.406 秒 | [回执](../../tmp/case-clone-streaming-rl_wo_k3/clone-streaming-result.json) |

四组按字节读回 50,331,648 字节文件，另覆盖 Unicode 文件名、空文件、节点额度拒绝和暂存结束后释放。PostgreSQL/S3 实测 50,331,648 字节本地暂存对应同等节点预留。相同字节被新来源版本替换时返回 409。8 次实际 API 突停分两类，已知事务恢复后同名重试成功，未知文件保持原字节。所有测试资源已清理，模型调用与模拟调用均为 0。早期失败来自临时 SeaweedFS 的卷数配置，扩大测试夹具后四组复验通过。[复跑入口](../server/case_clone_streaming_acceptance.py)

### 公开算例复制的首次源扫描

`POST /api/v1/cases/{case_id}:copy` 先在短事务记录可写的来源、目标工作区、源算例元数据和事件序号，随后释放业务事务和账户写入额度，再按原规则读取源清单与正文。重新取得协调和额度后复核权限、来源与事件序号，确定来源仍有效才创建目标。目标构造和提交前的完整来源 SHA 核对沿用现有协议。这个流程覆盖公开单算例复制的首次扫描。调用方自行管理的事务继续使用原流程。 [公开路由](../server/app/routers/workspaces.py) · [复制服务](../server/app/services/case_admin.py)

每组验收都在实际读到首个 1 MiB 后暂停 40 MiB 源文件的首次扫描。旧版 SQLite/文件系统的独立写事务无法取得锁，公开条件保存返回 `409 storage_quota_busy`，释放读取后复制成功。修复版 SQLite/文件系统及 SQLite/认证 SeaweedFS 均允许独立写事务开始，同算例条件保存返回 200。暂停的复制随后返回明确的来源变化 409，实际构造记录为零，公开列表只有原算例。用原名重试各创建一个副本，实际读回新文本，41,943,040 B 大文件的 SHA-256 与该组来源一致。异主调用返回 404，两组节点预留归零。 [旧版业务回执](../../tmp/case-copy-source-fs-7jbevbzk/case-copy-source-lock.json) · [文件系统](../../tmp/case-copy-source-fs-7vyueuww/case-copy-source-lock.json) · [认证 SeaweedFS](../../tmp/case-copy-source-s3-kj4gsrre/case-copy-source-lock.json) · [最终执行器](../../tmp/vibeflow-regression-all-20261119-182245-624a49/summary.json)

旧版执行器因测试进程退出瞬间仍能连接端口而记录失败，随后以独立 Windows 进程和监听查询确认已退出。固定首轮保留了并发 200、来源 409 和无目标构造的实测，执行器读取刚安装的本地夹具回执时遇到 Windows 文件访问冲突，未执行余下检查。修正夹具收尾探测和回执交接后，固定版两组完整通过，另以独立检查确认两个 Windows API 和临时 SeaweedFS 进程及端口均已退出。资源保存在 D 盘。该阶段观测的可用空间为 C 盘 21.61 → 21.67 GiB、D 盘 378.78 → 378.18 GiB。模型调用为 0。 [旧版执行器](../../tmp/vibeflow-regression-all-20261119-181642-4e8328/summary.json) · [固定首轮执行器](../../tmp/vibeflow-regression-all-20261119-181940-ccdfd9/summary.json) · [隔离存储](../../tmp/s3-attempt-store-n08ojlfd/store.json)

### 公开工作区复制的首次源扫描

`POST /api/v1/workspaces/{ws_id}:copy` 包含算例时，先在短事务中核对所有权，按算例 ID 顺序记录现有成员、复制所需的算例字段和事件序号。该事务和账户写入额度结清后，再读取源清单与正文。构造前重新锁定原工作区及其成员，复核权限、工作区成员和原版本绑定，再执行原额度检查。目标构造、每个算例及全工作区的最终 SHA 校验和持久恢复仍使用原提交协议。`include_cases=false` 只创建工作区。 [复制服务](../server/app/services/case_admin.py) · [公开入口验收](../server/workspace_copy_source_lock_smoke.py)

实际验收从两个原成员开始，在真实 40 MiB 源件的首个 1 MiB 块读到后暂停。旧版文件系统在暂停期挡住独立 SQLite 写事务，正常按原 SHA 条件保存返回 `409 storage_quota_busy`。释放后工作区复制返回 200，两份目标算例和大文件摘要均符合来源。修复版在文件系统和认证 SeaweedFS 的相同窗口内，普通保存返回 200，独立写事务可开始，另一次公开请求也能新增源工作区成员。两次较早的复制均明确报告来源变化 409，实际构造计数为零，公开工作区列表无目标。随后同名重试只创建一个工作区，完整读回三个成员、更新后的文本和与来源 SHA-256 一致的 41,943,040 B 大文件。 [旧版业务回执](../../tmp/workspace-copy-source-fs-2sxujz16/workspace-copy-source-lock.json) · [修复版文件系统](../../tmp/workspace-copy-source-fs-4tl_4pzf/workspace-copy-source-lock.json) · [修复版 SeaweedFS](../../tmp/workspace-copy-source-s3-2lcc4x1l/workspace-copy-source-lock.json)

两个后端上的仅工作区分支均返回 200，读取块和构造成员数均为零。异主请求返回 404，节点预留归零。旧版正式验收 1/1，15.719 秒，固定版正式验收 1/1，141.125 秒。Windows 进程、端口和 WSL 原存储进程均已独立核对退出。隔离数据保存在 D 盘。该阶段观测的空闲空间为 C 盘 21.41 → 21.49 GiB、D 盘 378.13 → 377.63 GiB，模型调用为 0。 [旧版执行器](../../tmp/vibeflow-regression-all-20261119-184020-419088/summary.json) · [固定执行器](../../tmp/vibeflow-regression-all-20261119-184221-908eb8/summary.json) · [隔离存储](../../tmp/s3-attempt-store-0ejj8269/store.json)

## 2026-11-19 构造前恢复期限与维护续扫

新建、导入、复制和备份恢复继续在分配新目标前按原顺序处理旧暂存与待决清单。本次为这段恢复复用存储测量期限。锁等待、扫描和清单检查共享该期限。到期后排空已启动的文件系统或 SDK 操作，再返回可重试的 409，后续请求从持久记录继续恢复。[构造恢复](../server/app/services/case_provision.py)

账户维护在锁外逐阶段读取暂存、待决清单和已启用保留规则的历史，每个阶段一次至多 32 个候选。到当前阶段末页后可继续下一阶段。进入锁后复核游标和原提交证据，再按原规则清理。私有游标和存储归属绑定，API 重启后续扫。账户维护响应的 `provision_scan` 返回阶段及下次从何处继续，原有结果字段保留。S3 直接分页，文件系统扫描直接子项并仅保留待处理的一页候选。[维护入口](../server/app/services/storage_maintenance.py)

四种实际组合各经历两次 API 突然退出。重启后续扫，四份异常清单逐字节保留，已核验旧前缀被回收，同名重试只创建一个算例。真实超时在分配新目标前返回 409。文件系统另核验实际安装硬链接先于待决清单处理。四组对维护证据调用真实 `deepseek-flash` 8 次，全部 HTTP 200，共 34,560 tokens，模拟调用 0。[SQLite 与文件系统](../../tmp/live-cli-startup-m1_h8zyf/constructor-maintenance.json) · [PostgreSQL 与文件系统](../../tmp/live-cli-startup-bfc6cwn8/constructor-maintenance.json) · [SQLite 与 S3](../../tmp/live-cli-startup-yrbowiyq/constructor-maintenance.json) · [PostgreSQL 与 S3](../../tmp/live-cli-startup-gs7qg57w/constructor-maintenance.json) · [复跑入口](../server/live_llm_constructor_maintenance_acceptance.py)

显式启用历史保留期限时，另实测三页回收四份过期的已提交回执，源算例仍可读，异常历史原样保留。该组模型调用为 0。[历史清理复验](../../tmp/constructor-history-rmoo8q4n/history-maintenance.json)

补上到期前的最后一次写入检查后，复验最终 SQLite/文件系统和 PostgreSQL/S3。两组各 2 次真实 DeepSeek HTTP 200。构造恢复专项包含早期成功验证和最终四组合在内，共 14 次真实 HTTP 200、60,714 tokens，模拟调用 0。[最终 SQLite 与文件系统](../../tmp/live-cli-startup-dvljh68a/constructor-maintenance.json) · [最终 PostgreSQL 与 S3](../../tmp/live-cli-startup-px2h88xc/constructor-maintenance.json)

## 既有构造提交协议

后续更新：创建暂存归属、启动/周期扫描、恢复历史保留、上传完成残留及账户诊断已接通，详见 [存储维护](storage-maintenance.md)。下文保留第九批的交付范围与证据。

2026-11-05，N2-06 第九批。四个入口已接入与新建算例相同的创建清单及 SQL 提交证明：ZIP 导入、算例复制、包含算例的工作区复制、工作区备份恢复。主线仍为 23 完成、11 进行中、19 待开始，剩余 30；本批交付四个构造入口的恢复子项，不关闭完整 N2-06 或原 295 项功能。

## 行为

导入和复制先生成最终的字典、时间线与修订内容，再保存全部目标文件的大小和摘要，逐文件读取并写入新目录。复制保留原有源算例锁、运行中作业拒绝、配额及来源版本校验；工作区复制先锁定全部来源，所有子算例及其提交证明在同一个数据库事务内提交。

备份恢复沿用原会话、分支、作业日志的 ID 映射和清单校验，每个目标算例单独记录文件清单，整个工作区一次提交。归档中的历史时间线和修订文件仍按原内容恢复。空工作区没有文件清单，仅提交工作区元数据。

四个入口与显式恢复接口均先获得账户锁。确认数据库回滚后不再递归删除目标目录；下次构造或调用 `POST /api/v1/case-provisions:recover` 时，凭清单和 SQL 证据决定如何处理。已经提交的目标不会被重放，因此提交后用户修改的文件得到保留。未提交目标仅清理清单中内容未变化的文件；发现额外文件或外部修改则保留现场。完整协议见 [创建恢复](case-provision-recovery.md)。

ZIP 原有 2,000 文件、50 MiB 单文件及上传、200 MiB 解压总量上限保持；备份原有 10,000 文件及容量上限保持。创建使用独立清单，不受普通文件事务 256 文件批次上限影响。已有算例复制的文件校验也不继承普通写入的 1 GiB 限制；仍按单个文件读取内容，没有改成整算例加载。未实测大于 1 GiB 的文件，不能据缩小边界测试声称该容量已验收。

## 验证记录

| 范围 | 通过 | 原始证据 |
| --- | --- | --- |
| 文件系统 + SQLite：导入、算例复制 | 12 组 | `../../tmp/constructor-recovery-evidence-v_3t0hxy/checks.json` |
| 文件系统 + SQLite：工作区复制、恢复 | 11 组 | `../../tmp/constructor-recovery-evidence-s0fq_o30/checks.json` |
| 文件系统 + PostgreSQL | 10 组 | `../../tmp/constructor-recovery-evidence-s_jouce3/checks.json` |
| SeaweedFS 4.52 + SQLite，独立 API 缓存 | 17 组 | `../../tmp/constructor-recovery-evidence-v3t48vek/checks.json` |
| SeaweedFS 4.52 + PostgreSQL，独立 API 缓存 | 10 组 | `../../tmp/constructor-recovery-evidence-ph0_qvvb/checks.json` |
| 四个入口各含 300 文件；可选快照容量与默认限制独立 | 5 项 | `../../tmp/constructor-capacity-evidence-fi0x_so4/checks.json` |
| 原算例/工作区复制、备份恢复 | 2 套 | `../../tmp/N2-provision-constructors-initial/summary.json` |
| 原 ZIP 导入、恶意归档拒绝及真实导入算例求解 | 1 套 | `../../tmp/N2-provision-constructors-import-compat/summary.json` |
| 原新建：正常、首文件后、提交前后 | 4 组 | `../../tmp/case-provision-evidence-mz4itkjm/checks.json` |

60 组故障验证按后端选取提交前后、首文件、首个子算例、异常回滚、提交确认丢失、来源变化、目标外部内容等场景，不表示每种组合跑过全部场景。两个 PostgreSQL 临时库已删除，开发库未迁移。最早两批 SQLite 运行后，测试补充了已提交目标的逐文件摘要检查及外部目标完整快照比较；后续 PostgreSQL/S3 运行使用加强后的断言。

原并发套件中的复制、恢复和新建故障注入仍指向旧写入函数，现已改为实际的创建写入或封存点；回滚断言通过正式恢复接口检查所有目标文件清理和数据库行回滚，永久目录占用记录及恢复回执不计入业务文件集合。原始失败日志保留。并发套件最终 SQLite 93 项、PostgreSQL 93 项通过，临时库已删除；详见本批汇总中的 `write-coordination.json`。

汇总与源码绑定：`tmp/mainline-N2-constructors-evidence/`。测试入口为 `case_constructor_recovery_smoke.py`；默认运行文件系统/SQLite 故障场景，`--s3`、`--postgres` 切换后端，`--capacity` 运行文件数量及缩小容量边界验证。

## 剩余工作

1. 账户创建暂存文件、空目录、历史记录、上传残留的保留与清理；目录占用证明不能简单按时间删除。
2. 启动扫描和旧无证明目录诊断，缺少归属证据时保留数据。
3. 原任务中的未知提交、旧准备文件补偿、跨主机和全部恢复组合；索引对账与相应后续主线保持原要求。
4. 原 S3 UDF 当前引用偶发 409 仍未归因，不能用这批创建恢复验证代替修复。

文件系统校验与删除之间仍可能有不遵守应用锁的宿主写者竞争；本批覆盖进程中断与数据库提交窗口，不宣称任意硬件掉电原子性。

## 2026-11-20 复制满载时的 WSL 终端续租

SQLite 搭配文件系统和认证 SeaweedFS 各完成一轮。两个算例复制和两个工作区复制各读取 40 MiB 实际源文件。四个线程在首个 1 MiB 读完后暂停，独立 SQLite 写事务仍可取得，第五个复制返回 429，未进入正文读取。最后一个请求到达隔离配置的 22 秒期限时，仍持有名额直至正文排空。排空前的新复制返回 429，排空后过期请求返回 504，重试返回 200 并读回原摘要。

同一非特权 WSL 终端在复制满载期间和过期线程排空前各执行一次实际命令，UID 均为 65534。两个窗口各观察到一次约 5 秒间隔的数据库心跳更新。复制结束时保留该活跃终端原有的磁盘预留，终端退出核验后节点预留归零。认证 S3 首轮的测试 WebSocket 在准备数据时关闭，复验使用持续收包处理心跳帧。真实模型 0，模拟 0。[文件系统](../../tmp/copy-workers-fs-w6h9sgk5/workers.json) · [认证 S3](../../tmp/copy-workers-s3-oh2bhgtk/workers.json)

### PostgreSQL 复验

隔离 PostgreSQL 搭配 Windows 文件系统和认证 SeaweedFS 各完成一轮。四条 40 MiB 复制在首个 1 MiB 后暂停。共享名额满时，第五条复制和同账户另一算例的文件保存均返回 429，另一算例改名返回 200 并读回已经提交的名称。过期复制排空前仍拒绝新复制，排空后返回 504，随后重试返回 200 且源摘要一致。首轮曾将共享名额的文件保存判为应当成功，保留原 429 证据。[文件系统复验](../../tmp/copy-workers-fs-ni156bns/workers.json) · [认证 S3 复验](../../tmp/copy-workers-s3-h97j29q8/workers.json) · [首轮 429](../../tmp/copy-workers-fs-oyms2ds2/workers.json)

两轮均在满载和过期排空时由 UID 65534 的真实 WSL 终端执行命令，数据库心跳各向前约 5 秒。终端退出码为 0，清理证明通过，节点预留最终归零，两个临时 PostgreSQL 库均删除。此组合真实模型 0 次，模拟 0 次。[文件系统库清理](../../tmp/terminal-agent-postgres-rl2z4igk/cleanup.json) · [认证 S3 库清理](../../tmp/terminal-agent-postgres-w39ovsq4/cleanup.json)
