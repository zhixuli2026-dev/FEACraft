# 来源校验与原生会话准入

2026-11-03，N5-01占用协议继续接入算例绑定的终端、CLI、可视化与NativeRequest。Job尝试、其余直接文件写入和正式发布接线仍待完成；没有把可编辑来源的哈希提前移出原来的发布事务。

## 已复现的窗口

基础占用版本 `mainline-N5-case-validation-20261103.zip` 中，真实CMD终端先接收一条延迟写文件命令，随后通过测试入口取得校验占用；命令继续写入当前算例，而 `require_current_locked` 仍通过。证据 `tmp/validation-native-before-hk1ze3jm/before-result.json` 保存实际根/gate身份及核验完成的停止回执。此为尚未接入正式发布的协议窗口，不声称线上曾发布错误成果。

前置脚本 `tmp/check-validation-native-before.py` 绑定基础版本源码字节，只允许在修复前运行。修复后不要重跑或用修改后的脚本替代原证据。

## 双向互斥规则

所有算例绑定的原生记录登记都取算例控制锁，并在插入前检查 held 校验占用。终端的选择和登记使用不同事务，因此登记函数自身检查，覆盖“选择已完成、校验刚取得占用”的窗口；CLI、viz、request无论调用者是否已经取锁，都保留登记层检查。全局探测/会话的case为空，不凭空建立算例绑定。

取得占用时，在同一控制锁下查询并验证四类历史记录，保留该锁直到占用提交。新的登记因此不能穿过查询与提交的间隙。已登记的starting/active状态覆盖后续启动和持续写入期间；租约过期不等于进程停止。

| 原生记录 | 允许取得校验资格的证据 | 需要继续阻止的状态 |
| --- | --- | --- |
| NativeSession终端 | closed/failed，完整且verified的停止回执，无暂存残留 | starting/active、过期、未知/损坏journal、缺少停止、未提交收尾 |
| NativeCliSession | closed/failed，核验过的停止或类型明确的not-started回执，无暂存残留 | 活动、过期、未知停止或待回写 |
| NativeVizSession | closed/failed，核验过的停止回执，无暂存残留 | starting/active、过期、failed但没有停止证明 |
| NativeRequest | 请求已终结，全部已登记子执行均具有核验停止；或终结且没有子执行 | active、过期、损坏journal、仍有preparing/running或未核验停止的子执行 |

NativeRequest的空终结journal可以通过，是因为 `native_execution.py` 在 `_prepare`/spawn之前等待 `records.reserve` 提交；请求终结后 `_current` 不再允许登记子执行。终端没有同样的“空记录代表未启动”证明，所以failed且无停止回执仍阻止校验。

NativeRequest的最终控制发送增加case检查，并保持“case锁→request行”的顺序。终端/viz/CLI继续使用各自已登记的完整生命周期保护，未把WSL身份探测搬进新的长case事务。心跳、关闭及停止回执提交不因占用而禁止；暂存回写仍通过原内容协调器。不得先释放校验占用，再等待仍在运行的读取线程结束。

## 验证范围

`validation_native_smoke.py --postgres` 在双API、共享隔离存储下分别通过SQLite/PostgreSQL各35项，结果 `tmp/validation-native-evidence-kz3zjjtf/result.json`：

- held占用拒绝四类新原生记录，数据库行数保持不变。
- 四类starting/active、过期和损坏记录阻止取得占用；CLI明确未启动回执与NativeRequest空终结journal允许继续。
- 真实终端覆盖选择与登记之间、登记与spawn之间、活动期间、进程已停止但回执未提交、核验关闭后的两实例互斥。
- 历史终端的缺失停止、未登记、三类暂存残留、非法暂存前缀仍阻止；已核验失败停止及已清理暂存允许继续。

终端使用真实PTY进程和停止回执。其余三类登记专项使用合法形状、不会spawn的隔离记录；不能把这部分称为真实CLI模型推理、真实VTK渲染或Agent工具执行。现有生命周期兼容套件另行验证正常原生路径。

前三次新专项失败及修复均保留：CLI夹具需计算实际launch profile摘要；未启动CLI仍有结构化journal，清理不能只接受空字典；viz整数ID需与测试路由字符串键正确转换。分别见 `tmp/validation-native-dual-first.log`、`tmp/validation-native-dual-second.log`、`tmp/validation-native-dual-third.log`。成功运行见 `tmp/validation-native-dual-fourth.log`。没有放松产品配置或停止验证。

现有兼容验收共五套完成：Agent shell32项、CLI会话36项、真实PTY44项、真实可视化33项及占用协议32项（这些兼容专项为SQLite）。初始汇总 `tmp/validation-native-compat/summary.json` 为4/5，保留可视化原始失败；单独复核 `tmp/validation-native-viz-recheck/summary.json` 为1/1，不能把初始结果改写成5/5。

现有可视化套件发现旧断言：主动注入late_lease时期待500，但原有 `ExecutionConflict` 契约为409/recovery_required。`tmp/validation-native-viz-fixture-review.json` 核对该测试与前一源码包相同、租约校验函数和全局错误处理未变；修复后专门断言错误码，原始失败日志保留在 `tmp/validation-native-compat/viz_execution_smoke.log`。完整复核结果为 `tmp/viz-owned-evidence-g2opwzhb/result.json`。

## 尚未完成的接入

- Job自身及子执行、连续日志、封存/发布准备的写入分类和停止检查。不能只排除当前Job就把其他Job全部当作安全；也不能让多个finalizing任务相互等待。应明确保存已经核验的停止事实，再区分独立产物目录和当前算例文件。
- 所有绕过 `case_write.coordinate` 的内容写入边界，以及该协调器中的其他只读入口；全局case为空的终端/CLI仍具有其操作系统权限，本协议不是对任意路径的OS隔离。
- 真正的来源/递归依赖图锁外验证、完整绑定复验、续租与独立撤销，以及真实作业慢校验期间的心跳/取消验收。
- 输入封存、别名修复与远端暂存的写入恢复协议。

head仍为 `5f3eb0a4d976`、36张业务表；本批没有迁移开发库或重载端口8000。主线53项仍11完成、18进行中、24待开始（剩42）；全功能295项按原完整标准继续验收。正式SIWC联调暂缓，其余目标继续。
