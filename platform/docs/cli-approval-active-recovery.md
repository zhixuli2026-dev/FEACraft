# 真实CLI审批交互与活动回合恢复隔离

2026-11-09，N4-05、FF-F30/FF-F41增量。主线37/53完成、剩16项，资源11/17完成、剩6块。本次修复恢复读取的一致性，并记录真实命令审批证据；N4-05及整个CLI功能均未关闭。下一主线推进前置已完成的N5-04，N4原剩余范围继续有效。

## 产品缺陷及修复

`tmp/live-cli-approval-y8675278/live-result.json`中，真实DeepSeek生成命令审批后，原API实际退出79，两个独立API并发恢复时出现`evidence_invalid`。最终数据库的回合、会话及恢复凭据摘要一致，不能据最终一致倒推当时没有失败。

独立SQL反例`tmp/cli-recovery-read-snapshot-fr9psbrq/result.json`在原库的副本上重现了读序列：读取者先加载awaiting_input回合，另一个事务实际提交已捕获的终态与凭据，原实现随后只查询新凭据，却用旧ORM对象校验，从而拒绝有效恢复。它不执行模型，复制的终态/凭据来自此前真实CLI，SQL前置状态单独标记为故障输入，不能替代真实CLI验收。

`codex_turn_recovery.evidence`现在用同一条SQL联查回合、会话及恢复事件，并刷新ORM对象。若之后读取执行锁时恰逢另一恢复提交，只允许已经验证且明确释放锁的凭据满足幂等返回；未释放的记录仍拒绝越过当前归属。更新后再次核对用户、会话、算例和会话关系。损坏摘要、未退出的owner、历史schema及重复凭据继续拒绝。

修复后`tmp/cli-recovery-read-snapshot-73z_0tkr/result.json`通过1项实际SQL交错和4项纯凭据协议检查。原库以URI只读方式复制，报告保存其未变摘要。

## 真实模型与实际交互

显式入口为`live_llm_cli_approval_acceptance.py --source-profile-id 26`，使用已安装WSL CLI和授权`api.deepseek.com / deepseek-flash`。模型实际选择工具，原始stdio传输实际发送决定；观察器转发原实现，不制造请求、模型回复、工具选择或推理结果。命令严格核验为隔离目录中的`cat Ref/approval-proof.txt`，返回内容未出现在提示词中。

| 场景 | FS/SQLite证据 | FS/PostgreSQL证据 |
| --- | --- | --- |
| 新回合仍等待审批时，双API重放旧恢复，保持新owner、token及resource | `tmp/live-cli-approval-5nllkjci/live-result.json`中的已通过子检查；整轮其后失败 | `tmp/live-cli-approval-z0za4f0_/live-result.json` |
| 两个并发批准仅一个成功、仅一次实际native回复，命令读回未知字符串 | `tmp/live-cli-approval-p3qf6gwe/live-result.json`中的已通过子检查；整轮其后失败 | 同上 |
| 实际审批取消、不执行读取、native interrupted、范围关闭 | `tmp/live-cli-approval-p3qf6gwe/post-run-cancel-proof.json` | 同上 |
| 等待审批时取消整个回合，收到实际native中断、审批过期、范围关闭 | `tmp/live-cli-approval-ity__res/live-result.json` | 同上 |

PostgreSQL入口整轮四组通过；SQLite四组由保留的实际分项组成，不把失败整轮改成通过。正常批准/取消的重复回复、一致的终态、一次claim/stream.done及迟到回复拒绝在相应完整报告中核验；SQLite补充只读取消审计不覆盖迟到HTTP回复。跨用户404、ask模式升级403及无效决定400属于纯HTTP协议检查。

八轮实际运行共启动14个CLI模型回合，包含故障中断和验收返工。CLI内的上游HTTP请求没有逐次观测，不能写成14次HTTP调用。按四个线程最后一次实际上报的total累计58,750 tokens；没有usage的中断回合不估算消耗。模拟LLM为0，未运行历史模拟回归。最终入口及导入辅助代码的审查见`tmp/cli-approval-review-v5-20261109.json`，总汇见`tmp/mainline-N4-05-cli-approval-recovery-evidence/validation.json`。

## 原样保留的失败和限制

- `y8675278`：产品读取混用导致有效凭据被拒绝；其报告中退出观察较早的API由`post-run-exit-proof.json`补充确认，原报告未改。
- `af8rrcek`：真实并发恢复成功后，验收脚本复用了重启前另一账户的JWT，收到401；脚本改为重新登录两个账户。
- `5nllkjci`、`x29xaq7s`：实际请求只提供accept、建议策略修订及cancel，脚本发送decline被正确400拒绝。应用继续使用真实availableDecisions；这些失败不是已验证的native decline。
- `p3qf6gwe`：真实批准已通过，随后实际cancel已完成；脚本把允许为空的aggregatedOutput当字符串，导致TypeError。修正判定，并对原真实取消结果只读补验，没有重新计模型回合。
- `xe4z801q`：S3/PostgreSQL并发恢复出现CAS state_conflict409，整轮未通过。当前实现为未对账S3暂存保留执行锁；没有把暂存推定安全或强行删除，也没有据此宣称S3恢复完成。原临时PG按夹具清理，未保存完整恢复行快照，因此不能补造精确竞争时序。

取消补验初版只执行查询和query_only，但普通SQLite连接关闭后主文件摘要改变，符合WAL回写；缺少事前WAL副本，原始主文件字节不变未获证明。失败live-result原文保持不变；v2采用URI mode=ro，保存并核验本次读取前后数据库摘要。该限制保存在补验报告中。

本单元两临时PG均删除、一自建S3停止；十八个已观察API出生身份退出、十三个API端口无匹配进程并拒绝连接，由归档总汇独立复核。只对已有凭据和实际观察作结论，不补造失败轮次未保存的完整原生停止记录。

仍需实际提供decline的模型请求、其他复杂审批和秘密输入、Windows模型回合、原生fork/resume、恢复提交中断、S3暂存的显式对账/释放及失败组合收口。原RB-12至RB-17和全部53/295目标保持。官方协议参考：[OpenAI Docs：App Server审批](https://learn.chatgpt.com/docs/app-server#approvals)。
