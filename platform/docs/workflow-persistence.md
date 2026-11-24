# 持久模型、状态机与事务事件（N1-01 / N1-02）

N4-03.g2b3e：CLI原生fork已接通持久准入、指定完成回合边界、双会话执行权、幂等查询、第一条消息原生续接、刷新发现及取消收尾；结果不确定时保留待恢复，禁止自动重建。分叉API双库各13、原生回合各40、迁移各147约束/34业务表、兼容5/5、实际Windows CLI本地Responses5项及浏览器12项通过，前端构建通过；UI独立复查NF-01已修正并复核。head2c0b87d1a643，开发库未迁移/重载；附件/多模态/完整模型/MCP/用量、正式SIWC/WSL/sandbox部署及N4/N5继续；53/295及11完成/16实施中/26待实施不变。 详见[原生CLI分叉](native-cli-forks.md)。

N4-03.g2b2：宿主Codex原生回合/SSE、幂等消息、复杂审批/计划问答、显式线程续接及API/Goal/审批共用执行归属已接通。双库各34回合、8暂存、6双API及115迁移约束通过；旧CLI会话各34、Agent shell各32、core14/14、协议辅助7/7通过，真实CLI本地Responses fixture6项通过，无远端推理或个人登录。head f97e54a8d310、30业务表；开发库旧13表未迁移/重载。下一单元g2b3账户登录/登出、sandbox状态/设置、WSL原生CLI与完整UI，随后专业入口、N4/N5；53/295和11完成/16实施中/26待实施不变。详见 [owned-cli-turns.md](owned-cli-turns.md)。

N4-03.g2b1：宿主CLI持久会话和正式API已接通；两级实际登记后放行，私有账户/临时目录、账户/模型展示、唯一线程创建、case/S3收尾与历史保护通过双库各34项、S3可控各9、迁移各87约束，原Agent shell各32及core14/14通过。真实Codex经正式路由读取空账户/8模型、创建线程并核验关闭，无模型推理。head e86d43f7c209、26业务表；开发库旧13表未迁移/重载。下一单元g2b2持久回合/SSE/复杂审批，再登录/WSL/UI及专业入口、N4/N5；53/295与11完成/16实施中/26待实施不变。详见 [owned-cli-sessions.md](owned-cli-sessions.md)。

API Agent shell增量：ToolCall唯一NativeRequest，原子审批、实际根/来源/权限复验及停止核验通过双库各32项，S3可控各9项。head d75c32e6b1f8、25业务表；g2实际Codex/CLI及完整N4-03继续，详见 [owned-agent-shell.md](owned-agent-shell.md)。

viz增量已验收：NativeVizSession、放行前根登记、实际kernel首帧、整树退出/缺回执、持久历史和copy/trash保护通过双库各33项；S3可控各9项，本机双API端口竞争通过。head c64b21d5a0e7、25业务表，完整N4-03继续Agent/CLI及专业入口。详见 [owned-viz-sessions.md](owned-viz-sessions.md)。

PTY后续增量：NativeSession按用户/可选case保存根与实际gate出生身份、租约及停止回执；真实重连、退出/缺回执、2MiB输入与脱离后代清理通过双库各44项，S3可控各9项。代码head b53a10c4f9d6、24业务表；详见 [owned-pty-sessions.md](owned-pty-sessions.md)。

全局Python/WSL能力探测已接入按用户所属、case为空的请求范围，固定用途、实际退出与降级语义通过双库各25项；不分配Job。代码head ad42f9b3e8c5，仍23业务表，详见 [owned-runtime-probes.md](owned-runtime-probes.md)。

后续SyncTeX用途迁移head9c31e8a2d7b4只扩展native_requests kind约束，仍23业务表；已有SyncTeX历史时拒绝直接降级旧约束，不删除记录。双库各35项约束、带数据往返、metadata diff=0通过，详见 [owned-synctex-requests.md](owned-synctex-requests.md)。

N4-03新增native_requests请求归属表，代码head8b20d7f1c6a3、共23业务表；它与Job提交/attempt分离，避免无效准备请求创建Job。双库迁移各34项约束、升级/回退/重升、旧Job保留及metadata diff=0通过。详见 [native-request-scopes.md](native-request-scopes.md)。下文首批证据保留历史范围。

日期：2026-11-02。N1-01、N1-02已验收；N1-04/05/06仍有剩余工作；后续Job执行接入和最新证据见 [job-execution.md](job-execution.md)。实际状态见 [执行台账](mainline-task-status.json)，契约见 [workflow-execution-contract.md](workflow-execution-contract.md)。全部53项主线和295项功能的目标保持不变。

## 当前已实现

`server/app/workflow_models.py` 在旧12表之外新增11表，`models.py`继续提供统一导入：

| 表 | 用途与主要约束 |
|---|---|
| workflow_runs | case/组织/发起用户、目标、请求哈希、输入清单、计划、预算、版本、取消与错误；case+用户+幂等键唯一，NULL键可独立提交 |
| workflow_steps | 处理器与版本、依赖、确认节点、输入/输出清单、active attempt、终态；run+step_key唯一 |
| job_attempts | 旧Job与根Job关联、重试来源、请求族尝试编号、phase/version、worker/fencing token、租约/心跳、进程身份、取消与原因、证据与发布清单；Job+编号唯一 |
| job_execution_heads | 根Job请求族的下一个编号与活动尝试；实际claim/重试使用，不用max()+1分配 |
| job_submission_requests | 普通Job提交的case+用户+幂等键、请求哈希、执行规格与原Job，跨重启回放 |
| native_requests | 准备请求的case/用户/owner出生身份、token摘要、固定profile、租约与子执行；不创建Job，终态/结束时间一致性约束 |
| artifact_publications | case+类别+代次唯一，不可变目录、清单哈希、关联attempt和提交状态 |
| artifact_publication_heads | case+类别活动指针/下一代次/version |
| case_event_counters | case序号分配器，事件与状态同事务占用序号 |
| durable_events | UUID、case序号、run/step/attempt关联、类型、JSON载荷；case+seq唯一 |
| event_outbox | 每事件唯一投递记录、领取token/租约、重试、错误类别与已投递时间 |

外键、状态CHECK、正编号/非负版本CHECK和查询索引已落实；`workflow_steps.active_attempt_id`刻意不建立循环外键，指针归属须由后续执行服务验证。表中存在租约/进程身份字段不代表已完成所有者恢复。run.org_id必须由授权上下文解析，新API不会接受用户任意指定组织。

`workflow_state.transition` 对Run/Step/Attempt执行版本与旧状态CAS：非法迁移拒绝；失败/取消/中断记录原因和结束时间；终态只能幂等读取，不能重开；已持有worker token的尝试拒绝迟到所有者，包括相同phase的无操作请求。Run成功要求所有步骤成功，blocked依赖保留原因。普通状态更新禁止将等待工程确认的步骤直接改成功，也不能自行领取worker；合法确认与claim分别由N1-03/N1-04接入。

状态、case序号、DurableEvent和Outbox在同一事务内写入；函数只flush，由调用者commit。失败的CAS不会留下事件或消耗序号。SQLite测试路径在SAVEPOINT前确认真实父事务存在，避免Python3.11驱动的旧事务模式使RELEASE提前提交，依据 [SQLAlchemy官方事务说明](https://docs.sqlalchemy.org/en/20/dialects/sqlite.html#transactions-with-sqlite-and-the-sqlite3-driver)。此处理没有改变长连接读/SSE的全局事务模式。

## 已接入的实际Job路径

实际Job的领取/阶段/终态由`job_execution`验证所有者并与事件同事务提交；兼容单项/批次取消和当前启动中断对账经`jobrunner._persist_status`写事务事件。Case失败回退仍为既有专业逻辑；尚未宣称文件与DB联合原子提交。

API启动事务outbox投递循环，关闭时限时退出。领取/确认使用token及租约CAS，投递异常指数退避，只记录错误类别；已提交Job不因通知失败被改成失败。崩溃后的过期投递可重领，事件ID保持稳定。旧jobs.updated字段保留，额外提供schema/event_id/seq/关联ID/payload，原WS/内存eventbus继续使用。暂无新增工作流REST入口或页面。

投递语义是at-least-once。消息发送后、确认前死亡会再次投递同一ID，不能承诺任意消费者恰好一次。当前本机API协调者负责投递；这不是多个API副本之间的事件广播。幂等业务消费者、文件树/索引联动、客户端游标和完整断线对账仍由N1-06/N6补齐。

## 迁移与现有库保护

首个迁移`0fd328dae4cf`，前置`6fb6c18fb7ee`，只增加9表；后续`4c02b1a7d9e0`再加提交请求表及attempt根Job/重试关联，合计22业务表；`7a91c4e2d6b0`再为attempt增加child_processes_json非空列及{}默认值；`8b20d7f1c6a3`新增native_requests表，合计23业务表；`9c31e8a2d7b4`扩展其kind CHECK支持只读SyncTeX请求；`ad42f9b3e8c5`支持用户全局runtime请求、NULL case与scope CHECK；`b53a10c4f9d6`新增native_sessions；`c64b21d5a0e7`另增native_viz_sessions；当前head`d75c32e6b1f8`增加Agent工具绑定/唯一性，仍25业务表，详见 [原生子执行](owned-native-children.md)。不改旧Job/case/user列。迁移文件是固定DDL，不调用运行时代码动态生成旧版本结构。

应用只对空SQLite开发库便利建表；空PostgreSQL也必须显式执行Alembic。已有业务表但缺新表、或已有Alembic版本与代码不一致时，启动明确要求经过审查的迁移，不会用create_all静默补表。SQLite连接同时开启外键约束。

本轮只读核对现有开发PostgreSQL：12业务表+alembic_version共13表，版本仍为`6fb6c18fb7ee`，9张新表均不存在。**没有升级该开发库、没有重启原开发API，也没有对生产库迁移。** 下一次切换新运行版本前应完成备份和迁移步骤。不得把初始化保护关闭后混用旧Alembic版本与新表，也不得将空开发库便利建表当作生产迁移流程。

回退演练只针对临时库：downgrade删除新增运行历史，实际有新历史的环境必须先导出、验证保留策略，不能直接降级清空。源码回退使用独立版本归档并保留新数据，不覆盖用户case。

## 首轮证据（最新claim/迁移26项见job-execution.md）

| 验证 | 实际范围 | 项目根目录证据 |
|---|---|---|
| workflow_migration_smoke --postgres | 临时SQLite/PostgreSQL，各20项模型/约束检查；旧库启动拒绝隐式升级；upgrade、重复upgrade、回退至旧版本、再升级、base往返；旧Job保留；Alembic metadata diff=0 | tmp/workflow-migration-72rpc8xh/result.json |
| workflow_state_smoke --postgres | 临时SQLite/PostgreSQL，各14项检查；并发CAS唯一获胜、状态/事件/序号/outbox整体回滚、三实体终态原因/时间/幂等、token防迟到、确认旁路拒绝、跨case/凭证拒绝、真实进程投递后崩溃/同ID重投、并发outbox领取 | tmp/workflow-state-sf3_5y5p/result.json |
| durable_outbox_api_smoke | 独立API真正被杀后，使用相同DB/JWT/fs重启未修改的生产app；2条已提交Job事件从pending补投到delivered，ID/序号/Job成功记录不变 | tmp/mainline-N1-durable-evidence/final-offline/durable_outbox_api_smoke.log |
| 回归 | offline16、core14、execution3、真实CAE3；Python入口总数174，未执行全集；本轮无新前端/浏览器验收 | tmp/mainline-N1-durable-evidence/ |

两组PostgreSQL验证使用不同随机名称的临时数据库，验证后删除；报告不记录连接串、密码或JWT。真实CAE仍为二维空腔、三维空腔、线性导热，数值断言不放宽。

空PostgreSQL拒绝自动建表的最终补充验证在`tmp/workflow-migration-hx0py690/result.json`：双数据库20项约束与全部往返迁移再次通过，PostgreSQL的`empty_postgres_requires_explicit_migration=true`，开发库未动。

## 下一实施单元

N1-03依赖/失败传播/确认核心已验收，见 [workflow-scheduler.md](workflow-scheduler.md)；N2-01协调器已验收，继续N2-02剩余入口及封存输入策略，见 [case-write-coordination.md](case-write-coordination.md)。N1-04已完成普通请求幂等、原子claim和实际JobAttempt阶段，专用入口和受冻结输入约束的重试仍待补齐；不会仅凭表结构自动产生完整工作流。原启动孤儿政策仍会处理所有queued/running，未解决存活worker所有者问题。不能把本轮outbox补投等同于求解器重启恢复或V18整项完成。
