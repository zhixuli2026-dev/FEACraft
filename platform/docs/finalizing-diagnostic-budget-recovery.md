# 收尾恢复的诊断增长、未知输出窗口与原期限

2026-11-10：为N5-04和N4-05补齐实际诊断文件增长、原额度耗尽、自然到期及待归档取消的组合验收。本批只修改隔离验收入口，产品源码与上一份已封存源码一致；没有把验收代码返工记为产品修复。主线37/53，剩16，新增整项0；资源11/17，剩6，53/295原目标保持。

## 实际行为

隔离报告先调用真实Pandoc。原任务提交前固定实际预算：到期场景60秒，超额场景32768字节，其余保持正常预算；这些参数实际写入原封存执行规格，不改历史用量或时钟。独立编译实际完成、用量记录提交后，向原诊断inode追加随机字节并fsync，再实际退出API。后续样本被真实用量锁阻止提交，恢复器必须从已停止的原文件补计增长，并保留4096字节未确认管道窗口。

| 场景 | 结果 | 证据 |
| --- | --- | --- |
| SQLite，8192字节未记录诊断增长、实际API退出 | 补计诊断并保留4096未知额度；同原任务恢复成功 | `tmp/professional-finalizing-kax5mzcb/professional-result.json` |
| SQLite，32768字节增长超过原32768字节总额 | 原任务failed/resource_limit；新收尾请求无原生child、无prepared成果 | `tmp/professional-finalizing-h9xcnebf/professional-result.json` |
| PostgreSQL，8192字节增长，等待原60秒期限自然到期 | 原任务failed/resource_limit；用量记录timeout，未续期或获得新原生额度 | `tmp/professional-finalizing-g0qgsxlu/professional-result.json` |
| PostgreSQL，原期限自然到期，待归档补传仍活着时取消 | 活补传阻止收尾；实际线程结束后原attempt取消完成，原pending清单及完整未知容量保留 | `tmp/professional-finalizing-4795xhis/professional-result.json` |

四组均保持原Job/attempt、封存输入、绝对期限和原物理stop；重复及双API请求收敛。已耗尽/到期的专业收尾不再编译、求解或发布。原已消耗字节不减少，4096字节未知窗口不被改记为已观察输出，也不被清零。原child缺失的stop继续缺失；OwnerExited只更新失败元数据，独立守护范围关闭证明另存，未伪造原历史。

取消场景中，被注入Task取消的补传HTTP请求实际返回500，保留该结果；另一个显式收尾请求随后完成取消。该场景不把原取消优先级改成超时失败，也不把远端结果未知解释成归档成功。

## 失败与复核

`professional-finalizing-1zt32u5b`和`professional-finalizing-_v94akte`两份实际运行判定失败。原业务任务当时已正确结束为resource_limit，但新断言误要求无原生工作的元数据请求本身也为failed；实际契约是记录操作正常完成、原任务失败。只修正验收断言，保留原业务失败、无child、无prepared、用量与发布不变的要求；随后分别新建隔离环境重跑通过，旧报告不改记成功。

首份独立复核脚本错误要求整个child文档完全不变，拒绝了合法的OwnerExited phase/error/ended_at更新。修订版逐项保持原identity、stop及其余字段，要求旧缺失stop仍为空，并核验实际guardian封闭、成员完整和活动进程0。原复核脚本与失败不删除、不重跑冒充原通过。

汇总：6个真实原生根各启动一次，4组通过、2组失败保留；23个实际API出生身份退出，6个自建S3 owner实际退出，3个自有临时PG数据库在系统目录中独立确认不存在，开发库未修改。审查登记`tmp/N5-diagnostic-budget-review-v1..v3-20261110.json`覆盖91份源码，包含导入的辅助代码；未运行历史mock LLM集合。

本子阶段真实模型调用0、模拟LLM0：没有测试模型回复、工具选择或推理回合。此前实际模型兼容证据仍为`tmp/professional-finalizing-75keau6w/professional-result.json`的DeepSeek两次HTTP200、8554 tokens，仅作既有参考，不重复计入本批。全部产品源码未变，未用固定回复替代模型验收。

最终独立证据：`tmp/N5-diagnostic-budget-verified-evidence-v2-20261110.json`；封存验收：`tmp/mainline-N5-04-diagnostic-budget-evidence/validation.json`；源码包：`platform/backups/mainline-N5-04-diagnostic-budget-20261110.zip`。

## 后续原范围

原四条remaining保持：证据生成、同步或专业校验中断且尚无完整证明时的恢复；历史LaTeX缺失物理停止证明的恢复或明确处置；其余处理器、FS、legacy、旧staging/恢复目录和已关闭归档预留竞争。下一步实现原生进程停止后、证据/日志/清单完成前的持久阶段登记和同attempt恢复，不能重跑求解器、不能给旧记录补造原worker证明。


2026-11-10（证据/日志/清单前中断恢复）：原worker在原生实际停止后、写证据前持久登记阶段及逐文件计划；匹配的部分暂存可接续，无覆盖安装。新preparation-recovery查询/POST入口要求原API确切退出和独立IO归属，新准备v3/来源v2保留原worker未完成的事实，再接原归档/完整校验/原发布；启动不再误中断有效阶段。8组FS/S3、SQLite/PG、Pandoc/WSL LaTeX/真实OpenFOAM接续通过，求解器不重跑；原正常/取消兼容2组另列。真实DeepSeek累计4次HTTP200、17090tokens，最终代码2次8546tokens，模拟LLM0。两份实际失败及一份观察格式复核失败保留；14个原程序根、23实际API及8自建S3 owner退出，4临时PG独立确认删除；4只读库15项负面与旧v5历史通过。主线37/53、剩16，新增整项0；资源11/17、剩6，原N5-04四条remaining和295目标不变。继续独立恢复API自身的取消/退出/竞争及并发边界、阶段资源/缺失缓存、历史LaTeX缺证明确处置及其余处理器/legacy/staging/已关闭预留竞争。 见[后续实际交付](early-finalizing-stage-recovery.md)。
