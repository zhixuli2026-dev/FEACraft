# 专业子执行在准备登记前失联的恢复

2026-11-17，N4-07 原有失联恢复条件的实现增量。主线暂仍 48/53，剩 5 项；本页不代表 N4-07 已完成。

## 实际缺口

原 `child_launch_registration.prepared_identity` 只接受完整 prepared 或 WSL 已提交的 admission。真实 child 已建立 guardian、但仅有启动意图或部分启动观察时，恢复器无法识别原范围，导致已退出 worker 的任务无法完成收尾。根执行已有相应恢复路径，专业 child 尚未接入。

## 实现

- 读取原 child reservation、意图与观察链；完整观察可提供原 gate 身份，部分观察只提供已知 host 和原范围。没有观察时，host 保持未知。
- guardian 的独立观察绑定完整 child 历史摘要，防止将完整历史截短成更早阶段或更换已观察出生身份。
- WSL 使用原 child、parent nonce、distribution 和已分配内核范围对应的撤销回执。未知 native gate 身份与退出结果保持 null；不能以宿主退出代替 Linux 范围停止。
- 原 running、prepared、admission、stop 缺失事实保持不变；原产物保留，不重放失败命令。取消请求仍按原时间和归属收尾。

本增量要求原 guardian 已登记，WSL 原分配已确认。更早的 child reservation、分配未确认和 guardian 未登记交接尚未由本次实现证明，不扩大声称覆盖范围。

## 验证入口与进行中证据

显式入口 `platform/server/live_llm_child_observation_loss_acceptance.py` 使用实际 PostgreSQL、Redis、S3、Windows API、Windows/Linux worker、Pandoc 与非特权 MPI。故障钩子等待实际启动边界后让原 worker 以 17 退出；首条观察缺失场景在 SQL 提交前等待，独立测试观察不传给生产恢复器。

计划核验 Windows 首条观察缺失、created、host_ready；WSL host_ready、native_ready 后取消；以及两类原 prepared 路径兼容。报告检查旧 HTML/DOCX 保留和新正文发布；MPI 检查双进程 400 单元结果、UID65534 和新发布。凭据篡改拒绝检查属于纯协议检查，与模型验收分开统计。

完成上述流程后，入口调用授权的官方 DeepSeek 服务，让真实模型选择文件工具读取新发布证明并续答；没有模拟模型回退，不调用 Claude。

当前执行证据：`tmp/remote-worker-mq2nw0er/`；源码审查：`tmp/N4-07-child-observation-review-v1-20261117.json`。整轮已通过，结果与独立清理核验如下。

## 最终结果

2026-11-17 N4-07 专业子执行部分启动恢复已通过：Windows首条观察缺失、created、host_ready及WSL host_ready、native_ready后取消，均使用原guardian、完整原观察链和原内核范围完成恢复；未知出生保持null，不补写缺失prepared/admission/running/stop。旧HTML/DOCX及求解场保留，新报告正文和非特权双进程400单元求解发布通过；Windows/WSL原prepared兼容通过。共35个实际任务、2次官方DeepSeek deepseek-flash模型调用、模拟0；204项纯凭据拒绝检查另列，180个记录进程出生身份独立核验退出。专属Redis/S3停止、临时PG删除且访问规则恢复。主线48/53、剩5，295项交付状态不整体提升。专业child更早分配/守护登记交接、其余外部owner及N4-05/N7原范围继续。

汇总证据：`tmp/mainline-N4-07-child-observation-evidence/validation.json`；源码审查及7组原始实际结果均保留。
