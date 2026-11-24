# 业务启动许可与早期中断恢复

2026-11-07。新增持久业务启动许可，修复此前“范围已经分配或关闭，但缺少业务出生记录，无法判断是否允许重试”的早期窗口。SQLite与PostgreSQL各四个真实API退出位置已恢复成功；已经提交许可却缺少宿主出生身份的情况继续阻断，没有扩大未知身份清理权限。

主线仍33/53完成、20项未完成；资源块11/17完成、6块未完成；295项全功能最终验收保持。本阶段关闭的是上述任务中的具体窗口，不代表整个资源和恢复任务完成。

## 新记录与恢复规则

`sandbox_image_scope.py` 新增 `admitted` 事件，在已提交allocated之后、调用业务 `OwnedProcess.prepare` 之前提交。它绑定完整allocated回执的摘要。新的scope事件使用 `vibeflow.image-native-scope.v2`；事件ID仍采用原有命名规则，同一个请求的事件版本必须一致。

新请求的完整阶段为 `planned → prepared → allocated → admitted → closed`。创建/登记/放行的检查要求admitted已存在；不能先创建业务进程再补许可。closed摘要覆盖之前的所有阶段，包括许可状态。

对于新版本，若admitted尚未提交，且范围已关闭、没有业务出生观察或已登记identity，可以判定业务进程**没有获准创建**。这不是声称某个未知进程已经退出。对已知的分配控制者仍核验实际Windows宿主和Linux创建者出生身份退出；只有planned的请求没有被补写虚构的控制者身份或原生回执。独立对账绑定整个scope历史和此种未许可依据，保留原child identity/stop为空的日志。

旧v1记录没有这一许可边界。读取时保留原四阶段形状及原摘要，不把缺少admitted解释为未启动；旧记录的关闭回执与独立恢复凭证仍可校验。混合版本或只降级一条计划事件会被拒绝。

admitted已经提交时，即使API实际在创建业务宿主之前退出，也不能从空的身份日志推断没有创建。当前恢复只撤销准确的原生范围，然后继续因业务宿主身份缺失返回409。完整Windows范围证明或更精确的原子创建依据仍待接入。

## 实际退出矩阵

通过 `live_llm_image_scope_startup_acceptance.py --source-profile-id 26`，以及其 `--postgres` 选项，在生产请求路径进行实际 `os._exit(81)`。没有替换模型回复或CLI推理，没有用合成数据库请求冒充实际创建。

| API退出位置 | 退出时持久状态 | 实际恢复结果 |
|---|---|---|
| planned提交后 | 有唯一计划，未运行分配控制器 | 确认业务未许可；保留无控制者/描述符的事实，继续重试 |
| prepared及启动意图提交后 | 实际创建者已观察；perform尚未发送 | 创建者实际退出后空分配检查返回不存在；没有宣称删除 |
| 原生分配成功、allocated提交前 | 内核范围真实存在；数据库仅有prepared | 测试独立观察范围确为空；恢复只凭数据库prepared找到并移除实际范围，未使用测试旁证里的descriptor |
| allocated提交后、admitted提交前 | 描述符已持久保存，业务未许可 | 按准确descriptor撤销，重试完成 |

[SQLite证据](../../tmp/sandbox-recovery-ui-retirement-0wh9a5d2/image-scope-startup-checks.json)和[PostgreSQL证据](../../tmp/sandbox-recovery-ui-retirement-qfp0hvbl/image-scope-startup-checks.json)各通过5项实际基础设施检查及1项协议故障：四处退出、四条旧请求不伪造出生/stop的整体恢复、降级schema后完成查询/重试/算例校验全部409。恢复准确原记录后状态回到完成，不重复删除。GET在重启后始终只查询，不触发撤销或对账。每轮保留恢复ZIP和原child日志。

另用 `--admitted-boundary` 在许可提交后实际退出：[阻断证据](../../tmp/sandbox-recovery-ui-retirement-yyfrg8ad/image-scope-startup-checks.json)。原生范围已实际撤销，但请求保持active、缺失的业务identity/stop未改写，原镜像保留，没有生成伪造的独立完成凭证，POST仍409。这组2项基础设施检查验证的是拒绝不充分证据，**不是该清理请求恢复完成**。

累计9次本阶段定点API退出。五条真实v1历史请求另经只读SQL连接验证：[历史兼容记录](../../tmp/scope-admission-history-compatibility.json)。其旧范围/对账摘要仍有效，`unadmitted_births`均拒绝把旧协议当成新的未许可依据；这是纯协议兼容证据，模型调用为0，不计作进程或内核验收。

## 真实模型与收尾证据

每组通过保留的恢复快照写回实际随机文件，再由真实模型选择读取工具并续答：

- [SQLite四处早期退出](../../tmp/sandbox-recovery-ui-retirement-0wh9a5d2/live-result.json)：2次，8425 tokens。
- [PostgreSQL四处早期退出](../../tmp/sandbox-recovery-ui-retirement-qfp0hvbl/live-result.json)：2次，8415 tokens。
- [已许可边界的阻断场景](../../tmp/sandbox-recovery-ui-retirement-yyfrg8ad/live-result.json)：2次，8385 tokens；证明保留快照仍可读取，不表示阻断的清理已完成。

合计 **6次真实 `api.deepseek.com` / `deepseek-flash` 推理，HTTP均200，共25225 tokens，模拟0**。三组严格仅回复凭证字符串均未通过，继续如实保留。

隔离端口52425、52676、52724均关闭，[临时PostgreSQL库已删除](../../tmp/image-postgres-6ncdiyo7/result.json)。收尾检查分别覆盖9、9、7个原有完整登记身份，均确认原身份不存在。SQLite有一条Windows PID复用：保留`mismatch`及后来出生时间，不改成`exited`；该组`all_recorded_processes_exited=false`、`all_original_identities_absent=true`。[原始收尾记录](../../tmp/sandbox-recovery-ui-retirement-0wh9a5d2/cleanup.json)保留这一差异。准备阶段的实际控制者退出另在退出矩阵和控制回执中记录。

开发库和schema未修改；旧审查哈希已失效的回归集合继续阻断。源码清单为357个服务源码、256个路由、370个静态边界；这不是完成率。

## 接下来仍须完成

1. 完整处理admitted已提交而Windows宿主出生尚未记录的窗口，以及host_ready缺少native出生的窗口。不得由原生范围不存在、PID消失或超时单独推断未知宿主范围全部结束。
2. 对新许可阶段测试双API竞争、重复取消、迟到创建/加入；验证取消发生在allocated提交完成而admitted尚未提交时的业务重试和额度结算。
3. 将已有created/native_ready真实API退出矩阵扩展至PostgreSQL，并覆盖撤销成功但closed回执未提交、恢复凭证提交前后中断。
4. 补实现升级、权限变化、WSL/宿主重启、空间故障和整请求统一资源期限。此次只兼容旧协议，不把未知历史实现自动升级为新保证。
5. 继续原镜像unlink/回执中断、其他暂存和失败副本、长期快照额度、完整目录/权限/删除/caseDict/大批量/可信基线恢复，以及剩余RB-12至RB-17和全部主线、全功能范围。

正式网站认证注册联调按用户决定暂缓。本阶段没有需要用户新增决定的事项。

后续进展见 [Windows 范围守护基础与真实模型复验](windows-scope-guardian-progress.md)：独立守护基础检查已通过，业务接入仍待完成；另已完成 2 次真实 DeepSeek 调用并保留严格输出格式未通过的事实。
