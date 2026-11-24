# 清理请求的原生作用域持久生命周期

后续：[业务启动许可与早期中断恢复](native-scope-admission-recovery.md)已为新v2记录新增admitted阶段，双库各四个早期API退出位置恢复通过；已许可但缺少业务身份的窗口仍阻断。下文保留首次业务接入的四阶段记录，最新协议及剩余边界以新文档为准。

2026-11-07。原镜像清理请求现已实际使用原生cgroup：持久保存创建前观察和创建回执后，业务启动门加入目标范围；正常结束、失败和取消均撤销范围并另存回执。显式重试可以在原请求已终止或实际所有者退出后撤销遗留范围，再核验已知进程身份并对账。

这已进入生产请求路径，不再只是独立控制器能力。**但更早的创建/提交中断、缺失业务宿主出生身份的窗口及完整Windows范围恢复仍未完成。** 主线仍33/53完成、20项未完成；资源块11/17完成、6块未完成；295项全功能最终验收目标保持。

## 请求记录和执行顺序

新增 `sandbox_image_scope.py`，通过既有DurableEvent保存四类独立记录，不改变数据库schema：

| 阶段 | 保存内容与放行条件 |
|---|---|
| planned | 与已保留子请求绑定的唯一控制请求，含分配种子、发行版及实现哈希；尚未创建目标 |
| prepared | 真实创建者出生身份、Windows宿主记录和目标不存在观察；与业务启动意图在同一事务提交，之后才允许perform |
| allocated | 实际descriptor及完整控制回执；匹配prepared后提交，业务门才能启动 |
| closed | 对前三阶段摘要的绑定及实际撤销回执；未许可的计划明确没有原生回执，不虚构描述符 |

所有事件绑定原请求、用户、算例、所有者出生摘要、子请求、父nonce、用途及参数摘要。启动意图增加 `scope_plan_sha256`，目标门nonce采用实际创建者派生值。只有一个创建者可以通过prepared事务；重复创建、过期所有权、已关闭范围和改变的实现均拒绝。

`OwnedProcess.native_scope` 将准确描述和固定辅助代码哈希传入真实WSL门。门在HELLO前加入范围；后续registration检查也拒绝内存中的范围选择改变。其他执行入口默认不带该参数。

`NativeExecutor` 新增资源收尾钩子，覆盖已经分配范围、却尚未创建业务进程的情况。清理请求在原有进程停止后执行实际范围撤销并持久保存closed，随后才写子请求停止及请求完成。撤销失败保留失败状态和未完成证据，不把同步close算作已核验收尾。

正常注册/放行要求已提交、尚未关闭的allocated；成功finish/complete要求closed。完成查询、旧读取者检查、算例校验和节点额度释放也核验closed，不能只看子进程的stop标志。

## 显式重试与独立证据

重试先授权用户与准确镜像，再在业务事务之外处理范围撤销。原请求仍活动时，必须核验实际API所有者已退出；已失败或取消且有ended_at时，允许所有者继续服务其他请求。操作前后重新核对完整请求绑定和分配历史；活动所有者、变更记录或未知观察不放行。

- 已提交allocated：按完整descriptor撤销，核验空且不存在。
- 只有prepared：通过此前的空分配恢复入口，要求实际创建者退出，只移除空范围。
- 尚无prepared：没有持久许可，记录未许可关闭，不伪造控制者或作用域出生证据。

**范围关闭不单独证明业务宿主已经退出。** 原有独立对账仍要求完整已知出生身份、原请求隔离条件和不变的启动证据。新的分配/关闭记录进入对账摘要，篡改回执会撤销完成和准入判断。原先未知的identity/stop日志保留，不补写成正常退出。尚未连接Windows完整范围证明，因此 `host_ready` 而没有native出生等历史窗口继续阻断。

## 本轮实际验收

使用本地已授权DeepSeek配置，仅隔离SQLite或唯一临时PostgreSQL库，未迁移开发数据库。所有模型回复、工具选择及续答来自真实上游，没有模拟LLM或推理CLI。

| 场景 | 证据 | 结果 |
|---|---|---|
| SQLite取消/正常结束 | [范围记录检查](../../tmp/sandbox-recovery-ui-retirement-lq2d4q6i/image-scope-checks.json) | 3项基础设施、1项故障；失败准备和成功重试各保存4阶段，真实内核确认两个范围不存在 |
| PostgreSQL取消/正常结束 | [范围记录检查](../../tmp/sandbox-recovery-ui-retirement-_pbuq4_i/image-scope-checks.json) | 4项基础设施、1项故障；另在真实native_ready时观察业务范围为populated，负载尚未放行 |
| SQLite真实API退出 | [启动恢复检查](../../tmp/sandbox-recovery-ui-retirement-b_3dmb_b/image-preparation-checks.json) | created/native_ready提交后API直接退出，两轮重启后显式撤销、已知身份对账和重试完成；3项基础设施、1项故障 |

前两组同时通过已有准备取消的3项基础设施和1项故障。篡改closed回执将GET完成查询、POST重试和算例校验全部变为409；恢复准确原记录后完成状态恢复，不重复删除。启动退出场景保留原子请求未登记日志，独立凭证绑定新的范围历史。[PG临时库已删除](../../tmp/image-postgres-6p4p1e8j/result.json)。

真实模型分别在实际恢复文件上选择 `read_text_file` 并续答：

- [SQLite取消链路](../../tmp/sandbox-recovery-ui-retirement-lq2d4q6i/live-result.json)：2次、8449 tokens。
- [PostgreSQL取消链路](../../tmp/sandbox-recovery-ui-retirement-_pbuq4_i/live-result.json)：2次、8561 tokens。
- [SQLite两次API退出链路](../../tmp/sandbox-recovery-ui-retirement-b_3dmb_b/live-result.json)：2次、8493 tokens。

合计 **6次真实 `api.deepseek.com` / `deepseek-flash` 推理，HTTP均200，共25503 tokens，模拟0**。三组严格仅回复凭证字符串均为false，未算通过。每组原有完整登记的9个进程身份均核验退出；额外范围控制者的两端退出证明在allocated/closed回执中。隔离端口51497、51758、51793均已关闭。

源码清单为357个服务源码、256个路由、370个静态边界；数量不是验收覆盖率。本轮未运行审查哈希已过期的历史回归集合，真实模型入口为 `live_llm_image_scope_acceptance.py` 和 `live_llm_image_preparation_acceptance.py`。

## 下一步及尚未覆盖的窗口

1. 对planned/prepared提交前后、perform许可、原生创建成功但allocated未提交、allocated提交后尚未启动业务门等位置逐个实际退出，确认恢复流程及节点额度结算。当前控制层已有空分配机制，业务层这些具体中断位置尚未验收。
2. 继续处理尚无业务出生观察的窗口。不能把“已经撤销原生范围”直接等同于“未知Windows宿主及其整个范围已退出”；需要准确的未启动边界或完整宿主范围证明。范围关闭后仍阻断的记录必须明确保留。
3. 将本轮SQLite的启动API退出矩阵推广至PostgreSQL；验证新持久阶段的双API竞争、连续取消和迟到加入。当前PG验证覆盖正常、实际准备取消及证据篡改，不代表上述并发矩阵已覆盖。
4. 补充撤销/closed提交前后退出、历史实现变化、权限变化、WSL/主机重启和空间故障；把控制器局部期限接入整请求资源预算。
5. 完成原镜像unlink/回执中断、其他暂存与失败副本、长期快照额度、完整目录/权限/删除/caseDict/大批量/可信基线恢复，以及剩余RB-12至RB-17和其他主线、全功能要求。

网站认证注册联调按用户决定暂缓。没有因本阶段通过而缩减53项主线或295项全功能交付范围。
