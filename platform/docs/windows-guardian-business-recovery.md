# Windows 守护机制接入业务恢复

后续 v4 已通过同进程原生预登记解决新请求的 `host_ready` 未登记窗口，双库六处真实退出验证见[原生预登记与交接](native-preflight-handoff.md)。下文保留 v3 阶段的原始实现范围和证据。

2026-11-07。原镜像清理现在会在业务进程创建前启动独立 Windows Job 守护进程，并把实际准备身份提交到启动许可。解决了新请求在许可提交后、内核创建宿主后尚未接管句柄、宿主出生记录提交前的 API 退出恢复问题。SQLite 和 PostgreSQL 各三个位置已实际退出并恢复；没有补写不存在的原始身份或停止记录。

主线仍为 **33/53 完成、20 项未完成**；资源块 **11/17 完成、6 块未完成**；295 项全功能最终验收尚未逐项完成。本阶段关闭具体恢复窗口，不等于整个 N4-05 或资源交付完成。

## 实现与边界

新范围事件使用 `vibeflow.image-native-scope.v3`，阶段仍为 `planned → prepared → allocated → admitted → closed`。`admitted` 除分配摘要外还包含守护请求及实际准备记录，绑定原 API 出生身份、业务 nonce、固定实现摘要和当前清理请求下的独立证据目录。

`OwnedProcess.prepare_guardian` 先创建空 Job 并复制真实句柄给范围外守护进程；守护成员观察就绪后才能提交许可，再进行原子 Job 内创建。登记及释放前要求守护仍活着。正常停止通过守护关闭后续入场、排空实际成员、观察所有成员退出，并保留守护创建句柄退出证明。`scope_observation` 继续使用实际成员统计，未更改成固定返回值。

正常清理还要撤销 Linux cgroup；v3 的 `closed` 同时绑定原分配历史、原生撤销及完整 Windows 守护证明。即使守护失败，finally 仍尝试撤销已经拥有的原生分配；缺少守护证明时不得提交完整关闭记录。

显式重试先核验旧所有者退出或终态请求隔离，再只读检查已选守护实际退出及其固定文件回执，撤销原生范围，最后在事务中重新检查请求状态并提交完整关闭记录。GET 不执行这些操作。守护缺失、回执截断、成员未闭合或绑定不符都继续阻断。

当没有任何 `created` 观察时，固定宿主尚不可能获得首次 WSL 放行。守护回执证明所有可能的 Windows 成员均退出，包括尚未登记出生的宿主；此时才能独立对账。已有 v1/v2 的返回结构和摘要不增加 guardian 字段，不把历史缺证据解释为新保证。

**仍未解决的窗口：`host_ready` 已提交而 native 出生尚未记录。** 原生门可能处于启动和加入 cgroup 之间，不能只因 Windows Job 结束或 cgroup 不存在就声称所有未知原生进程退出。该分支继续拒绝恢复，后续需要原生启动完整范围证明。

## 实际验证

所有模型行为都调用已授权 `api.deepseek.com` / `deepseek-flash`；以下故障注入只结束真实 OS 进程或破坏隔离证据，不替代模型输出。

| 算例 | 实际结果 | 证据 |
|---|---|---|
| 正常业务和准备取消 | 活 API 下取消、重试清理、实际成员与 cgroup 撤销成功；9 个登记身份退出 | [范围检查](../../tmp/sandbox-recovery-ui-retirement-vaobnuqi/image-scope-checks.json) |
| SQLite 创建中断 | 三处真实 API 退出全部恢复；截断文件回执与篡改持久成员回执均拒绝，原 child identity/stop 保持为空 | [4 项基础设施、2 项协议故障](../../tmp/sandbox-recovery-ui-retirement-kzr8boxm/image-guardian-checks.json) |
| PostgreSQL 创建中断 | 同样三处真实 API 退出与两个证据故障通过；临时数据库已删除 | [矩阵](../../tmp/sandbox-recovery-ui-retirement-ebh6h1l4/image-guardian-checks.json)、[临时库结果](../../tmp/image-postgres-554__ive/result.json) |
| 守护进程实际被杀死 | 实际创建句柄终止；业务未创建、原生分配已撤销；重试和算例准入仍 409，镜像保留、没有伪造完成 | [2 项基础设施](../../tmp/sandbox-recovery-ui-retirement-a4wp30q6/image-guardian-failure-checks.json) |
| 独立守护基础 | 正常树、三个所有者退出位置、期限、守护被杀及回执故障复验通过 | [6 项基础设施、1 项协议故障](../../tmp/windows-scope-guard-accb1c5a54ee477380059987cfb5e64d/result.json) |
| 旧记录兼容 | 12 条实际 v1/v2 历史记录只读检查，原范围和独立对账摘要仍有效 | [只读协议证据](../../tmp/guardian-history-compatibility.json) |

真实模型证据分别为：[正常业务 2 次/8565 tokens](../../tmp/sandbox-recovery-ui-retirement-vaobnuqi/live-result.json)、[SQLite 2 次/8463 tokens](../../tmp/sandbox-recovery-ui-retirement-kzr8boxm/live-result.json)、[PostgreSQL 2 次/8618 tokens](../../tmp/sandbox-recovery-ui-retirement-ebh6h1l4/live-result.json)、[守护死亡场景 2 次/8631 tokens](../../tmp/sandbox-recovery-ui-retirement-a4wp30q6/live-result.json)。每组模型实际选择读文件工具并流式续答，HTTP 均 200，模拟 0。严格仅回复验证字符串均未通过；守护死亡组只证明保留快照可读，不代表阻断的镜像清理已完成。

早期 planned/prepared/分配成功未提交/allocated 未 admitted 四处退出也已针对 v3 复验通过：[5 项基础设施、1 项协议故障](../../tmp/sandbox-recovery-ui-retirement-hpi8_2cy/image-scope-startup-checks.json)，随后 [2 次真实模型调用、8520 tokens](../../tmp/sandbox-recovery-ui-retirement-hpi8_2cy/live-result.json)。本轮五组累计 **10 次真实推理、42797 tokens、HTTP 均 200、模拟 0**；严格仅回复字符串均未通过。旧历史证据保持原样，不用当前预期覆盖过去的真实结果。

本轮定点业务 API 退出共 10 次（两个数据库各三个创建位置，加四个早期分配位置）。隔离端口 54272、54603、54902、55203、55404 已关闭；正常/SQLite/PG/早期矩阵各 9 个原有登记身份退出，守护死亡场景 7 个退出。新守护及未登记宿主另有真实创建句柄或出生身份退出证据。源码静态清单为 360 个服务源码、256 个路由、373 个边界；这些数量不是功能完成率。

## 下一阶段任务

1. 完整证明 `host_ready` 到 native 出生登记之间的原生启动范围，覆盖迟到启动/加入、父进程先退出和守护故障，保持不完整证据时阻断。
2. 新协议下双 API 竞争、重复取消、迟到创建/加入；扩大 PostgreSQL created/native_ready 的真实退出矩阵。
3. 实际中断发生在守护回执已写但 closed 未提交、原生撤销已完成但回执未提交、独立对账提交前后；验证幂等性及原始日志不被改写。
4. 守护目录/文件变化、权限、实现升级、宿主/WSL 重启、空间不足，以及涵盖守护和控制器的整请求资源期限。没有当前回执的历史记录仍保守处理。
5. 继续原镜像 unlink/回执中断、其他暂存和失败副本、长期快照额度、目录/权限/删除/caseDict/大批量/可信基线完整恢复，以及 RB-12 至 RB-17 和全部 53+295 目标。

回退必须保留 v3 历史及拒绝规则；不能把已启动过的请求降级为旧协议或删除守护证据后自动重试。网站认证注册联调仍按用户决定暂缓；本阶段无需新增用户决策。
