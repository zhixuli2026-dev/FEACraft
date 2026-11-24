# 账户存储锁的冲突分类、等待期限与结算

2026-11-09，N4-05 / RB-14 增量。主线37/53完成、剩16项；资源11/17完成、剩6块。本次关闭可重复的账户锁故障，不关闭整个资源预算任务。

## 实际问题与修复

原实现中，SQLite的其他写事务占用和已过期读快照分别抛出未分类的SQLITE_BUSY（5）和SQLITE_BUSY_SNAPSHOT（517），可沿请求返回普通500。隔离库的原始证据是`tmp/owner-lock-before-qfktde7y/result.json`；原代码摘要与上个源码包一致。它们是实际SQL事务，没有模拟数据库或模型。

`storage_admission.lock_owner`现在把SQLite BUSY/LOCKED家族转换为409 / `storage_quota_busy`，其他数据库错误继续暴露。没有实际事务时先执行BEGIN IMMEDIATE；存在调用方事务时不替换、不提交、不回滚，不自动重放旧快照。SQLite仍然只有一个数据库写者，不声称可以按账户并行写入。

普通准入的绝对截止时间取当前请求剩余期限和`storage_measurement_timeout_seconds`（默认30秒）中的较短者。SQLite在同一连接临时缩短busy_timeout，普通尝试最多等待100毫秒，结束后恢复原值；实际驱动执行及恢复在重复取消下仍先排空。100毫秒是数据库等待上限，不是包含调度、连接获取和收尾的请求耗时承诺。PostgreSQL等待使用事务级try-advisory-lock和最多50毫秒的轮询间隔，共用一次绝对期限，不在每次轮询时续期。

既有工作在请求到期后仍需释放其持久预留。只有六个释放/对账入口显式使用`wait=True, settlement=True`：CLI初始化关闭、日志结算、输出归档结算、发布结算、远程写入结算及owner预算的独立收尾。它们保留原绑定与完成依据，以新的、仍有限的配置预算等待锁；不会重新批准资源。普通准入不享有该例外。结算锁预算耗尽仍如实失败，不伪造已释放记录。

## 当前源码的验收

| 范围 | 实际结果 | 证据 |
| --- | --- | --- |
| SQLite核心 | 10组通过 | `tmp/owner-lock-core-w21ktwqf/result.json` |
| PostgreSQL核心 | 8组通过 | `tmp/owner-lock-core-zdcleo5e/result.json` |
| FS/SQLite双API | 4组通过 | `tmp/owner-lock-api-rfzzs0k1/lock-result.json` |
| FS/PostgreSQL双API | 4组通过 | `tmp/owner-lock-api-2u554fmu/lock-result.json` |
| S3/SQLite双API | 6组通过 | `tmp/owner-lock-api-k4ilulud/lock-result.json` |
| S3/PostgreSQL双API | 6组通过 | `tmp/owner-lock-api-3gwjq8mx/lock-result.json` |
| S3分块上传兼容 | 4组通过 | `tmp/upload-resource-api-a917ia5b/resource-result.json` |
| FS旧共享上传归属兼容 | 9组通过 | `tmp/upload-owner-api-p1dt8mho/owner-result.json` |

核心覆盖实际持锁者、等待释放、配置/父请求期限、三次Task.cancel、原连接PRAGMA恢复和调用方未提交事务；SQLite另覆盖旧读快照与缺表错误。每个数据库另有一条显式已过期Deadline的纯协议输入。API覆盖独立进程持锁、实际文件读写、S3文件树准入耗尽返回409、真实请求到期后结算无活动pool，以及持锁API实际退出79后另一API重新获锁。上传兼容保留实际分块字节、双API完成竞争、三次取消、提交前后退出及原会话续传；故障输入及暂停位置在原报告逐项记录，不当作模型验收。

最终生产源码另通过`live_llm_acceptance.py --source-profile-id 26`：`api.deepseek.com / deepseek-flash`的4次真实chat-completions调用全部HTTP200，回执合计11,946 tokens，覆盖翻译、Agent流式持久化、模型选择读文件工具及续答。`/models`请求单独记录，不计作推理。证据`tmp/live-deepseek-pb8koaun/live-result.json`。此前源码版本的4次实际调用、11,965 tokens保留在`tmp/live-deepseek-_7dp51op/live-result.json`；两轮共8次、23,911 tokens，不把旧版本通过混称最终源码通过。模拟LLM为0，未运行历史模拟回归。

当前审查为`tmp/owner-lock-review-v4-20261109.json`、`tmp/owner-lock-upload-compat-review-v2-20261109.json`和`tmp/owner-lock-real-model-review-v2-20261109.json`，覆盖入口、导入辅助代码及生产模块摘要。验收总汇与源码包校验见`tmp/mainline-N4-05-owner-lock-admission-evidence/validation.json`。

## 返工、清理及未完成范围

两次失败保留原状：初版SQLite零毫秒尝试使实际S3上传完成后的短暂全局写锁产生409，见`tmp/upload-resource-api-xdti2klk/resource-result.json`，随后加入有限100毫秒宽限并通过兼容复验；另一次验收夹具在真实rollback后读取已过期ORM User，导致MissingGreenlet和500，见`tmp/owner-lock-api-6uaa8lfy/lock-result.json`。后者只修夹具预先保存标量ID，未放宽产品准入；只读SQL证明5个pool均已settled、没有active，见同目录`post-failure-SQL-proof.json`。

本单元七个唯一临时PostgreSQL数据库已删除，八个自建S3实例均停止，失败轮次也包含在内。实际API出生身份退出、所有本单元API端口及临时数据库不存在的独立复核写入总汇。既有真实模型辅助入口未记录API出生身份，因此只报告其端口无匹配进程且拒绝连接，不补造出生证据。开发库只读获取既有模型配置，未迁移、未写入。

这次修复不保证全部SQLite错误均已解决，也没有完成所有路由的统一总期限、连接/网络不可中断I/O、心跳、全局队列/内存或OS硬边界。上个浏览器阶段的runtime probe通道关闭尚未定位；本次独立SQL反例不代替其归因。N4-05原remaining、RB-12至RB-17以及53项主线和295项功能目标完整保留。
