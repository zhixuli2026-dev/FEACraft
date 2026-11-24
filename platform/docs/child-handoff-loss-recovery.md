# 专业子执行分配与守护登记交接恢复

2026-11-17，继续 N4-07 原始关闭/失联恢复条件。当前九组实际场景通过，首轮容量失败与续验分别保留；N4-07其余原始条件继续。

## 缺口与实现

先前恢复要求 child 已有守护登记。child 预留已提交、但原生范围分配未完成，或守护进程建立后尚未登记时，任务无法取得完整收尾依据。

现在 child 的原预留事件保存启动实现摘要与原有放行顺序：守护和 running 登记完成后才允许业务执行。旧事件仍可读取，但不升级成新放行约定。根分配已有的 `retire`/`abandon` 路径扩展为选择指定 child；原分配历史、已知创建者退出、完整父守护停止回执和原 child 历史共同参与恢复凭据核验。

原 v4 排空凭据保留，新增明确的 child 交接恢复类型和 closure 清单。没有子守护登记的 child 必须在这份清单中逐一闭合；其他原生范围仍按原完整清单核验。未知创建者或守护身份保持未知，不补造原 running、admission、stop 或退出码，不重放业务命令。

恢复提交仍在锁内复核原身份、输入、完整交接历史和取消请求。旧发布保留，完成后由明确的新请求生成新报告/求解结果。

## 本轮实际验收

入口：`platform/server/live_llm_child_handoff_loss_acceptance.py`。审查：`tmp/N4-07-child-handoff-review-v1-20261117.json`，含生产和导入辅助代码的 639 个源码散列。

在真实 PostgreSQL、Redis、S3、Windows API、Windows/Linux worker 下核验：

- Windows 子任务预留后退出，以及实际 guardian 返回但登记尚未提交时退出。
- WSL 分配意图、准备记录、分配返回未确认、已确认分配，以及未提交 guardian 后取消。
- 同版源码核验 Windows 首条观察缺失、WSL 完整 prepared 的原恢复路径。

钩子调用原实现，仅在指定真实边界等待后让 worker 退出 17。未确认分配和 guardian 的实际身份仅写入独立测试证据，不传给生产恢复器。每组还检查旧产物保留、新 HTML/DOCX 正文或非特权双进程 400 单元求解发布。Windows/Linux 凭据拒绝检查属于纯协议检查，单独统计。

先由真实 DeepSeek 读取实际基准发布文件，整轮结束后再读取恢复后发布文件；没有模拟模型回退，不调用 Claude。当前目录：`tmp/remote-worker-rewbm_3z`。首轮仅前置两次真实调用通过；失败及后续补齐结果见下。

独立核查发现的 API 终端正常关闭缺口另见 [执行归属核对](N4-07-owner-closure-audit.md)，不以本轮 child 场景代替该修复。

## 首轮实际失败与续验

首轮 `tmp/remote-worker-rewbm_3z` 最终失败，不能按整轮通过计。Windows 两组、WSL 分配意图/准备/返回未确认/已确认四组已通过；第七组在写入输入文件、尚未触发故障钩子时收到真实 S3 PutObject InternalError。SeaweedFS 日志明确容量已满且没有可分配卷：隔离辅助程序只配了 3 个 32 MB 卷，宿主 D 盘仍有约 490 GB 可用。

失败证据及原 639 份源码已归档到该目录；122 个记录进程出生身份独立核验退出，Redis/S3 停止、临时 PostgreSQL 删除、临时访问规则恢复。前置真实 DeepSeek 调用 2 次，整轮末尾读回未执行；模拟模型与 Claude 调用均为 0。

续验入口 `platform/server/live_llm_child_handoff_completion_acceptance.py` 使用新隔离环境和 16 卷容量，只执行剩余 WSL guardian 返回后取消、Windows 观察缺失兼容、WSL prepared 兼容及原远端基线。生产源码保持相同；前六组保留为部分通过证据，不重写首轮失败。续验已通过，目录 `tmp/remote-worker-pf7hnc52`，审查 `tmp/N4-07-child-handoff-completion-review-v1-20261117.json`。

## 最终结果

2026-11-17 N4-07 专业子执行分配与守护交接恢复通过9组实际场景：Windows预留/guardian返回未登记、WSL分配意图/准备/返回未确认/已分配/guardian返回后取消，以及原Windows观察缺失和WSL prepared兼容。首轮因隔离S3容量不足失败，保留六组通过；生产源码不变，在扩大测试容量的新环境补齐余下三组和基线。两轮共47个实际任务、官方DeepSeek deepseek-flash真实调用6次，模拟0、Claude 0；纯凭据拒绝检查300项另列，202个记录进程出生身份独立核验退出。最终独立检查首次201个PID不存在、1个Windows PID出生改变；该原出生在首轮清理时已核验退出，定向复查时PID也已不存在，原检查失败与补充观察分别保留，未放宽生产停止证明。旧产物保留，新报告及非特权双进程400单元求解发布通过；专属Redis/S3停止，临时PG删除且访问规则恢复。主线48/53、剩5；295项交付状态不整体提升。下一步修复已实际复现的API正常关闭遗漏终端清理，N4-07仍未整体关闭。

汇总证据：`tmp/mainline-N4-07-child-handoff-evidence/validation.json`。
