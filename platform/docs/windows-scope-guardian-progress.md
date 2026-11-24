# Windows 范围守护进程基础与真实模型复验

后续已完成业务接入和 SQLite/PostgreSQL 三处实际创建中断恢复，见[业务恢复进展](windows-guardian-business-recovery.md)。下文保留基础实现阶段的原始范围与证据。

2026-11-07。本阶段完成独立 Windows Job 守护进程的基础验证，尚未接入业务请求恢复。主线仍为 33/53 完成、20 项未完成；资源块 11/17 完成、6 块未完成；295 项全功能最终验收标记未增加。

## 真实模型证据

执行 `platform/server/live_llm_image_scope_acceptance.py --source-profile-id 26`，退出码 0。使用隔离 SQLite、存储和端口 53815，复用已授权 DeepSeek 凭据，未修改开发库。

- 实际服务 `api.deepseek.com`，模型 `deepseek-flash`，2 次推理请求，HTTP 均 200，共 8379 tokens，模拟调用 0。
- 第一轮模型实际选择 `read_text_file`，读取 WSL 实际产生、经恢复快照写回的随机文件；第二轮根据工具结果流式回答。提示中没有提供验证字符串。
- 对话及工具结果已持久保存，恢复文件未修改。`strict_answer_only_proof=false`：回答包含正确字符串，但未严格仅输出该字符串，不将该项记为通过。
- [模型及上游证据](../../tmp/sandbox-recovery-ui-retirement-0_mzsy4e/live-result.json)、[范围验收](../../tmp/sandbox-recovery-ui-retirement-0_mzsy4e/image-scope-checks.json)、[收尾证据](../../tmp/sandbox-recovery-ui-retirement-0_mzsy4e/cleanup.json)。9 个登记身份均实际退出。

这次模型验收覆盖现有业务路径的兼容性，不能证明尚未接入的守护进程业务恢复已完成。后续所有模型回复、流、工具选择和 Agent 回合继续只调用真实服务；真实服务失败或不可用就保留失败/未验证，不回退模拟。

## 守护进程实现和检查

`windows_scope_guard.py` 保留守护进程的实际创建句柄，把尚未启动业务的空 Job 句柄复制给独立守护进程。`windows_scope_guardian.py` 在范围外观察真实所有者身份，从第一名业务成员进入之前记录 Job 成员生命周期。正常退役、所有者退出、控制管道关闭或期限到达时，关闭后续入场，终止范围并核验实际成员退出，写入绑定请求摘要、守护身份和准备记录的回执。缺失回执、未完成成员观察或守护异常退出均拒绝完成证明。

首次真实运行发现：守护程序写出回执后，后台线程仍在缓冲 stdin 上等待，导致 Python 解释器退出时崩溃。新增有限错误输出保存，确认 `_enter_buffered_busy` 后改用有长度上限的原始管道读取。失败证据保留在 `windows-scope-guard-72402dd88aa44ff4a5ba6ba102534697` 和 `windows-scope-guard-b312408d47fa4836870e67f53cc9c225`，没有把已写出的回执当作正常退出证明。

审查当前入口和辅助代码、更新该入口的精确源码哈希后，执行 `windows_scope_guard_checks.py`，退出码 0：[最终证据](../../tmp/windows-scope-guard-11084aaab3154200bb5ff3f428ead363/result.json)。

| 分类 | 数量 | 内容 |
|---|---:|---|
| 实际基础设施 | 6 | 根进程及后代关闭且范围外进程不受影响；所有者在空范围、内核创建后尚未接管句柄、进程树已启动三处实际退出；守护期限到达；守护实际被终止后拒绝完成证明 |
| 协议故障 | 1 | 未退出成员、错误守护身份、错误请求绑定被拒绝 |
| 模型调用 | 0 | 本检查只启动真实 OS 进程，不生成模型回复，不替代以上真实 DeepSeek 验收 |

成员总数取实际 Job 记账并要求全部观察闭合，不把业务根及后代两个已知身份当作全部成员的固定数量；已知业务身份还单独核验退出。清理错误为空。

## 下一步

1. 将守护准备记录和固定证据目录绑定到业务启动许可，在创建业务进程前持久提交；目前此部分未接入。
2. 正常关闭和独立恢复读取完整守护证明，同时验证守护出生身份实际退出、原生范围撤销及请求所有权。
3. 通过真实 API 退出覆盖 admitted 后未记录宿主出生、host_ready 后未记录 native 出生的窗口，并执行真实模型验收。缺证据仍保持 409。
4. 继续并发、取消、迟到创建、PostgreSQL 退出矩阵及[前阶段剩余任务](native-scope-admission-recovery.md)。宿主重启、外部结束整个进程树或守护证据丢失不能由本阶段推断安全恢复。

本阶段源码尚未冻结进新的完整源码 ZIP，上一份归档仍是 `mainline-scope-admission-20261107.zip`。当前工作树包含守护基础实现，不能把旧归档当作这些新增文件的快照。
