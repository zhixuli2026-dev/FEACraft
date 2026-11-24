# 原生出生预登记与同进程交接

2026-11-07。新清理请求在加入原生 cgroup、进入业务启动门之前，先报告并持久保存真实原生出生身份。原生助手收到绑定该身份的许可后，在同一个 Python/OS 进程中执行已经核验摘要的启动门代码，避免 `host_ready` 到 `native_ready` 之间再次创建未知原生进程。SQLite 和 PostgreSQL 各六个真实 API 退出位置恢复通过。

主线仍为 **33/53 完成、20 项未完成**；资源块 **11/17 完成、6 块未完成**；295 项全功能终验保持。本阶段解决新协议的具体原生启动窗口，不把历史缺证据的记录升级为已有完整证明，也不代表整个恢复、并发和资源目标完成。

## 执行顺序与持久证据

1. 分配原生范围并启动 Windows Job 守护。固定宿主在 Job 内创建，首次放行只启动 `native_process_preflight.py`。
2. 原生助手先固定会话身份，报告真实出生记录，等待最多 45 秒。此时不创建子进程、不加入业务 cgroup、不执行业务命令；EOF、错误许可或超时均结束。
3. API 独立核验原生身份仍匹配。新 `vibeflow.image-native-scope.v4` 的 `admitted` 原子保存 Windows 宿主、原生助手、守护准备及四份固定原生实现摘要。
4. 保存顺序仍为 `created → host_ready → native_ready`；这些是观察事件。v4 的只读助手已在许可前准备，不再用旧的“created 前没有原生助手”推断恢复。
5. API 发送绑定 nonce 和完整原生出生摘要的许可。助手重新核对实现摘要和自身身份，在同一解释器内执行已经核验的门代码。门加入原生 cgroup 后报告 `wsl` 握手，API 要求其出生身份与预登记完全一致。
6. 完整进程登记及最终 release 仍是业务命令执行的前提。门执行有 330 秒原生上限，Windows 守护原有上限仍保留；这不是整套统一资源期限已经完成。

v4 的不完整启动恢复绑定原启动意图、全部观察、完整范围历史和预登记身份。即使最后只有 `host_ready`，也能对准确的原生出生记录做实际退出核验；不能凭 PID、超时、cgroup 不存在或 Windows Job 退出直接宣称未知原生进程结束。原 child identity/stop 保持原样，另存独立对账凭证。

缺少 admitted 时仍只能证明**业务未获许可**，不是证明所有未登记的只读助手都已退出。创建、许可前的固定助手不获准进入业务范围；更广泛的助手额度、取消/超时及未知历史维护仍在资源任务范围内。

## 实际验证

| API 实际退出位置 | 已提交的最后观察 | 独立观察到的原生范围 |
|---|---|---|
| admitted 提交后 | 无 created 观察 | 空，原生助手尚未进入 |
| created 提交后 | created | 空 |
| host_ready 提交后 | host_ready | 空 |
| 原生许可发送后、接收握手前 | host_ready | 记录实际值，可为空或已有成员 |
| 收到原生握手、native_ready 提交前 | host_ready | 有实际成员 |
| native_ready 提交后、完整登记前 | native_ready | 有实际成员 |

[SQLite 六处矩阵](../../tmp/sandbox-recovery-ui-retirement-fa0v27pe/native-preflight-checks.json)与[PostgreSQL 六处矩阵](../../tmp/sandbox-recovery-ui-retirement-a76ogwmc/native-preflight-checks.json)各通过 **7 项实际基础设施检查、1 项协议故障检查**，随后各调用真实模型。每次实际 `os._exit(81)` 前核验准确原生身份还活着，重启后核验原所有者、宿主、原生助手及守护确实退出；独立对账还核验原分配控制者身份。最终正常请求的实际原生门身份与许可保存的助手身份完全一致。

故意更改持久预登记的出生时间后，完成查询、重试与算例准入均返回 409；还原准确证据后恢复原状态。没有补写虚构的原始停止记录，没有用合成请求冒充实际启动。GET 不触发收尾。

- [正常业务和准备取消](../../tmp/sandbox-recovery-ui-retirement-7rnw7ajz/image-scope-checks.json)：实际取消、重试、活范围观察和原生撤销兼容通过。
- [真实守护死亡](../../tmp/sandbox-recovery-ui-retirement-7r1tfcll/image-guardian-failure-checks.json)：许可提交后通过真实创建句柄杀死守护；已知宿主和原生助手退出、cgroup 撤销，但缺守护回执时重试与准入继续阻断，镜像保留。
- [旧记录兼容](../../tmp/native-preflight-history-compatibility.json)：23 条实际 v1/v2/v3 历史记录以 query_only 连接验证，原范围及独立对账摘要有效。此为纯协议兼容证据，不是新的进程或模型验收。
- [临时 PostgreSQL](../../tmp/image-postgres-9mjkyhfc/result.json)：迁移与矩阵在独立临时库完成，数据库已删除；开发库未修改。

真实模型使用授权的 `api.deepseek.com` / `deepseek-flash`：正常业务 [2 次/8416 tokens](../../tmp/sandbox-recovery-ui-retirement-7rnw7ajz/live-result.json)、SQLite [2 次/8494 tokens](../../tmp/sandbox-recovery-ui-retirement-fa0v27pe/live-result.json)、PostgreSQL [2 次/8427 tokens](../../tmp/sandbox-recovery-ui-retirement-a76ogwmc/live-result.json)、守护死亡 [2 次/8418 tokens](../../tmp/sandbox-recovery-ui-retirement-7r1tfcll/live-result.json)。均实际选择读文件工具、读取恢复的随机文件并流式续答，HTTP 200，模拟 0。严格只回复验证字符串均未通过。死亡场景只证明保留快照可读，不表示其阻断清理完成。

更早 Windows 创建位置的 v4 复验已通过：[4 项实际基础设施、2 项协议故障](../../tmp/sandbox-recovery-ui-retirement-crf1m6kb/image-guardian-checks.json)。覆盖许可提交、内核创建后/Popen 接管前、created 观察提交前的三个真实 API 退出位置；内核创建中断发生在 v4 业务许可之前，后两类记录按实际许可状态分别恢复，不伪造原 child 注册。随后 [2 次真实模型调用、8418 tokens](../../tmp/sandbox-recovery-ui-retirement-crf1m6kb/live-result.json)。旧创建/准备验收入口已调整为当前原生预登记流程；历史报告与源码归档未改写。

本轮五组共 **10 次真实 DeepSeek 推理、42173 tokens、模拟 0**，严格只回复字符串均未通过。定点业务 API 退出共 **15 次**（两个数据库各六个原生启动位置，加三个更早创建位置），另有一次实际守护终止。隔离端口 55968、56234、56699、57171、57362 均关闭；正常/SQLite/PG/创建矩阵各 9 个原有登记身份退出，守护死亡组 7 个退出。未完整登记的助手和守护身份另在各矩阵保存实际退出证据。

最终还增加已登记身份与 v4 许可身份的交叉核验：[13 条实际记录只读验证、6 次身份篡改拒绝](../../tmp/native-preflight-record-binding.json)。使用实际历史行的内存副本破坏元数据，不修改原数据库、不伪造进程或模型结果。该补强为纯协议故障验证，与之前的真实 API/OS/模型验收分开统计。

## 剩余任务与下一步

1. v4 下重复取消、双 API 竞争、迟到创建/加入、许可前后助手死亡，以及错误许可、原生超时和守护期限的实际矩阵。资源期限应覆盖整个请求和所有固定助手。
2. 守护/原生回执已生成但 closed 未提交、独立对账提交前后、原生 unlink/业务完成回执中断的实际退出与幂等恢复。
3. 权限、目录/文件变化、实现升级、WSL/宿主重启、空间不足；缺少可核验出生/范围证据的历史记录继续明确阻断。
4. 其他导出暂存、失败和未完成恢复副本、长期快照及完整额度结算；完整目录/权限/删除/caseDict/超过256文件/可信基线与三方冲突恢复。
5. RB-12/13 的原生 CLI 全生命周期、技能/远程导出和空间峰值；RB-14 至 RB-17 的长 I/O/队列、未知历史维护、全局并发、工作目录外与跨卷实际资源边界，及全部 53+295 范围。

回退保留 v4 记录和拒绝规则，不能删去预登记身份后按旧协议自动重试。正式网站认证注册联调继续按用户决定暂缓，无需新增用户决策。
