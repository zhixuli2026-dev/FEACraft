# 完整证据形成前的收尾阶段恢复

2026-11-10：已实现进程停止后、证据/日志/输出清单完成前中断的显式接续。保留原Job、attempt、封存输入、原物理停止、已用额度和绝对期限，不重跑原程序。原路径与恢复路径支持FS和S3；真实Pandoc、WSL LaTeX、OpenFOAM均已有通过证据。本批不关闭N5-04：主线37/53、剩16，资源11/17、剩6；原四条remaining和295项全功能目标保持。

## 持久阶段与文件计划

原worker在实际root及已登记child停止后、写证据和日志前，记录`finalizing_stage`及唯一原始事件。内容绑定原输入/worker/process/children、停止回执、已用额度、原日志摘要、原输出清单及原求解输入快照。事件只存摘要，不把大清单塞进事件，也不写入凭据。未知或缺失停止证明不能获得此授权。

证据日志、视图标记、求解证据JSON和最终日志都先提交文件计划。文件按计划写到本attempt的`finalizing-staging`，完整核验并fsync后以无覆盖重命名安装。已有目标只有与原计划完全一致才可复用；部分暂存只有逐字节匹配已提交数据的前缀才可接续。不同内容、链接或未登记输出均保留并报冲突，不覆盖未知文件。

启动对账识别有效的新阶段，保留finalizing和原发布预留，提供`recover_preparation`提示。旧的“没有发布意图则中断”规则继续用于没有有效阶段证明的记录。没有自动启动求解器或恢复任意回调。

## 显式接口与证明

1. `GET /api/v1/jobs/{id}/preparation-recovery`查询原阶段、原attempt及`stage_sha256`。
2. `POST /api/v1/jobs/{id}:recover-preparation`提交原attempt和阶段摘要。原API实际存活/未知时拒绝；确切退出后，新API先登记独立CaseStorageOperation，再验证封存输入、原输出及文件计划，补齐证据和日志。
3. 返回`captured`及`inventory_sha256`。S3继续通过现有`:recover-outputs`归档，再`:finalize-outputs`；FS直接沿现有完整收尾路径校验、发布。FS的`:restore-output-cache`只核验本地原清单，不把封存输入当作丢失的执行结果重建。

新API写出的准备证明为`vibeflow.attempt-output-preparation.v3`，来源为`vibeflow.finalizing-source.v2`，引用原阶段、原API确切退出、独立IO归属及捕获事件。`pre_capture_stages_returned=false`明确保留“原worker未完成”的事实；没有伪造原worker的原始capture事件。已完成数据的重复请求只读取原凭据。

完整收尾识别新来源事件，沿原发布/outbox/别名计划提交原publication；新API独立登记必要的文档校验，原求解器不重跑。恢复继承原输出额度、绝对期限和原存储策略，监测当前恢复节点；不编造旧节点峰值。旧读取器在IO前拒绝新准备格式，旧v1/v2及先前完成的v5记录仍有效。

## 实际验收

下列均使用实际程序和实际SQL/文件/S3操作，部分写入是真实原日志的前7字节并fsync，随后实际退出API。没有替换推理、编译器或求解器结果。

| 场景 | 后端/数据库 | 证据根目录（均在tmp） |
| --- | --- | --- |
| 报告清单形成前退出 | S3 / SQLite | `early-finalizing-9qdpjk88` |
| 报告日志部分写入后退出 | FS / SQLite | `early-finalizing-kdg1dpki` |
| 报告日志部分写入后退出 | S3 / PostgreSQL | `early-finalizing-pa_x2o4r` |
| 实际求解证据日志部分写入后退出 | FS / SQLite | `early-finalizing-g1gpm0fg` |
| LaTeX清单形成前退出 | FS / PostgreSQL | `early-finalizing-rgzymt83` |
| 部分写入恢复后真实模型读回 | S3 / PostgreSQL | `early-finalizing-58k49pjr` |
| 最终存储策略集成后的真实模型读回 | S3 / PostgreSQL | `early-finalizing-bvw829jz` |
| 实际求解证据生成前退出，最终代码 | S3 / SQLite | `early-finalizing-lbbbbfb2` |

八组均从原阶段接续到同一Job/attempt和原publication，当前发布头正确、重复响应一致。文档校验为新登记的实际子执行；两组OpenFOAM各先实际blockMesh、再实际icoFoam，恢复时不再运行求解器，真实末时刻场、求解证据与原日志均被核验。恢复前原API存活时均返回409。

原普通S3完成和原归档取消路径兼容另列：`attempt-output-normal-yav1oa6d`、`professional-finalizing-c0gwmu0z`。全部12次实际运行合计14个原程序根（包含两次独立网格准备），每个目标原程序只执行一次；23个实际API出生身份退出、8个自建S3 owner退出，4个自有临时PG数据库独立确认删除，开发库未修改。

真实模型均通过显式`live_llm_early_finalizing_acceptance.py --source-profile-id 26 --postgres`调用本地配置的`api.deepseek.com/deepseek-flash`。原文件在编译前包含随机验证串，提示中只提供前缀，模型必须实际调用read_text_file后返回完整串。累计4次HTTP200、4份用量回执、17090 tokens；最终源码2次、8546 tokens。模拟LLM为0，不使用历史mock回归集合。

## 原样保留的失败

- `early-finalizing-j7lia6ml`：原阶段已保存，但旧启动规则先将任务标成interrupted。修复为识别有效阶段、保留待接续状态；原失败库不回写或改记通过。
- `early-finalizing-tqzw3jmu`：FS原路径未捕获这份日志，部分写入故障点没有触发，任务正常完成而测试失败。修复为有效阶段下也计划并保存FS日志；旧无阶段FS保持原行为。
- 第一份独立复核脚本未处理旧版report专用入口没有`handler`观察字段的格式，出现KeyError。v2按该已审查入口的固定report类型读取，未重写原报告或放宽业务断言。

只读审计`tmp/N5-early-stage-readonly-audit-20261110.json`覆盖3份实际新FS/S3库与1份先前legacy v5库。15项旧读取器拒绝、原阶段/IO归属篡改、准备格式降级及冒充原阶段返回均被拒绝；四库mode=ro/query_only，前后字节摘要不变。纯协议负向副本不包含模型调用。

源审查登记`tmp/N5-early-stage-review-v1..v6-20261110.json`最终覆盖97份源码及导入辅助代码。独立实际证据为`tmp/N5-early-stage-verified-evidence-v2-20261110.json`；封存为`tmp/mainline-N5-04-early-stage-evidence/validation.json`及`platform/backups/mainline-N5-04-early-stage-20261110.zip`。

## 原范围继续

本批已验证原API退出；独立恢复API在部分写入/捕获提交后退出、HTTP取消、双API竞争及阶段请求全局并发限制仍待专项收口。还需覆盖阶段资源失败、缺失原缓存和已关闭归档预留竞争。支持任意未登记回调或旧共享staging不能以此批代替：当前明确拒绝自动重放，保留数据和可核查原因。

两份历史LaTeX缺失物理停止证明的恢复或明确处置、其余处理器/legacy/staging及N5-07人工操作继续按原目标推进，不能据本批将N5-04或53/295全目标标为完成。
