# N6-07 沙箱根范围恢复

2026-11-12。主线仍44/53，剩9项；295项全功能原范围不变，最终整项验收仍0。本增量是实际浏览器/API/Windows/WSL/SQLite验收，真实模型0、模拟LLM0。此前真实DeepSeek验收单列，不以本次基础设施测试替代。

## 改动与边界

普通沙箱命令现在先持久登记独立Linux cgroup分配，再登记Windows guardian和实际等待门进程，核验Linux范围中确有原门进程后才能放行命令。原工程子进程协议保持原schema；沙箱根使用独立的`vibeflow.job-sandbox-root-scope.v1`，child_id和parent_nonce均为空，不借用子进程身份。

固定沙箱启动器在建立命名空间、挂载和pivot_root前需要root；实际用户命令仍由原沙箱脚本以65534执行。普通完成、取消及启动失败均回收已分配Linux范围。服务重启后，必须同时具有原worker退出、独立Windows范围关闭、原Linux分配/准入记录、对应范围回收及原Linux门进程退出，才允许生成独立恢复证书。原process_stop缺失仍保持缺失。

适用范围限定为已有封存输入、无专业handler、无argv、无工作流步骤及发布预留的普通shell沙箱。其他执行类型不进入该新增分支。普通WSL尚未在本增量改变权限或接入根范围恢复。

## 实际验收

第一宿主`tmp/case-workflow-controls-poexpbm9`实际页面完成：作业28正常完成且输出uid65534；作业29在真实命令等待时停止API、同环境重启；刷新无POST重放，原尝试interrupted、原stop=null，生产校验通过独立恢复证书。页面确认创建作业30、number=2并绑定原尝试，再在命令真实运行后取消。冷刷新保留新尝试，375px日志高度439.42px。三个实际Linux范围的回收均有absent回执。宿主退出0，API/preview确切退出，旧历史和源数据不变。

随后收紧jobrunner选择条件，避免非普通shell的沙箱执行误入新协议。最终源码宿主`tmp/case-workflow-controls-5kpme9zf`重新完成整个运行中重启、恢复、页面重试、取消和刷新路径：原作业28、attempt `9065c753-f29a-4867-913a-91913ee2ed91`；新作业29、attempt `bf4c27ec-6a69-4972-b053-3b9b1d6c5676`，number=2、root_job_id=28。原stop仍null，新作业实际cancelled且停止核验通过。冷刷新无POST，原日志只有开始及65534，没有末尾完成标记。宿主退出0，原历史和源数据不变。

最终页面首次提交及第一次手动重试遇到实际409 `storage_quota_busy`，稍后沿用原键成功，只创建作业28；恢复后的第一次确认重试也遇到同类409，页面保留待核对请求，点击“继续同一次提交”返回202，只创建作业29。保留全部409和最初passed=false的操作观察，不将其改写成首次成功。这证明可继续操作，不代表存储竞争延迟已优化。

`tmp/job-starting-cancel-cjp4kxn4/starting-cancel-result.json`为最终源码启动取消验收：真实created观察已提交、命令尚未放行时请求取消，覆盖WSL批次、Windows单作业和沙箱单作业。三组均通过，同算例后续真实命令成功。沙箱已分配范围回收、禁止载荷未出现在日志，批次排队成员未领取或启动。API退出已核验，无清理失败。

`tmp/N6-sandbox-root-certificate-audit-20261112.json`只读验证第一宿主真实历史及证书，检查11个内存篡改拒绝条件，包括缺Linux回收、缺Linux历史、缺Windows范围、owner仍活、伪造原stop、Linux门未退出、冒用子schema、错误scope/guardian及未核验树。普通root WSL输入不被沙箱谓词接受。它是纯协议拒绝检查，不是模型模拟或额外实际重启。

审查记录：`tmp/N6-sandbox-root-restart-review-v1-20261112.json`、最终`v2`及`tmp/N6-sandbox-root-starting-review-v1-20261112.json`。实际执行前阅读完整宿主和testsupport，未执行历史混合模型集合。

## 归档前发现的宿主清理缺口

独立核查拒绝了上述两个宿主的“全部后台执行已停止”结论。`poexpbm9`和`5kpme9zf`各有2个本轮能力探测请求仍为active，原停止回执缺失。原因是测试宿主只等待作业收尾，页面最后一次冷刷新触发能力探测后便终止API。已记录身份的Windows根和Linux门经实际观察已退出；这只证明这些根退出，不能补造完整树停止回执。原始宿主passed=true仅覆盖当时较窄的作业检查，不再作为完整宿主验收通过的证据。

`tmp/N6-sandbox-host-drain-counterexamples-20261112.json`保持passed=false、whole_host_acceptance_passed=false。未知/准备中的身份与缺失回执原样保留，未改SQL。测试宿主新增对本轮原生请求的独立等待和验收，在目标API重启前和宿主关闭前均检查，最终passed还必须满足`all_actual_request_stops_verified`。这项改动不声称修复了任意能力探测突然中断后的恢复。

## 加入后台请求收尾检查后的最终验收

`tmp/case-workflow-controls-ss_ta2ea`重新运行完整路径，实际作业28在输出开始标记和65534后中断API；同环境重启，原attempt `3448975f-eead-4b92-9fa7-fb141a3900b4`保持interrupted、原stop=null，独立Windows/Linux恢复证书通过生产校验。冷刷新无POST，确认前作业仍只有28及旧历史；页面确认后创建作业29、attempt `b34ea943-456c-482c-9fe4-b482a111c4b8`、number=2。新命令实际运行后页面取消，停止回执verified=true；再次刷新保留第2次尝试，375px日志高度439.42px，无POST重放。

最终宿主退出0；本轮12个能力探测请求的12个真实子执行均完成并持有核验通过的停止回执，API和preview确切退出，原业务历史、源数据库和源索引不变。`controls-result.json`的`passed`与`all_actual_request_stops_verified`均为true。审查绑定`tmp/N6-sandbox-root-restart-review-v3-20261112.json`。此前两个宿主的4个缺回执请求继续作为失败证据保留，不能借用最终宿主的停止证明。

## 下一步与用户决定

用户已明确选择普通WSL命令采用非特权执行、优先完整恢复；已记录到根AGENTS.md。下一步接入普通WSL的受控根范围，验证实际uid、工作目录读写、正常完成、启动取消、运行中服务中断及页面重试。不能只把沙箱谓词放宽为WSL：普通WSL载荷必须在放行前降权。

仍需处理原生根范围分配/登记中断的部分启动窗口，保留能力探测突然中断后的未知停止记录，并核对N6-07原四条remaining及完整浏览器七项条件；本增量不宣布N6-07整项完成，也不把N7全阶段矩阵重复扩入N6。主线2～3周的低置信度估计仍以11月12日为起点，未自动顺延。
