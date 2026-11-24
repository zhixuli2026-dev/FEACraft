# 技能永久目录的存储核算与写前准入

2026-11-09：N4-05 的一个实现单元，主线仍为37/53完成、剩16项；资源预算11/17完成、剩6块。本单元不关闭N4-05，不改变53项主线和295项全功能的原范围。

实际旧码反例保存于 `tmp/skill-storage-before-fix-1v5d73iy/result.json`：个人和组织技能目录各600KiB，合计1,228,800字节，但节点库存只看到1字节，个人用量为0；节点上限1MiB时仍放行额外256KiB。反例只操作隔离文件和真实节点预留，不调用模型。

## 实现

`VIBEFLOW_SKILLS_STORAGE_ROOT` 可指定技能根目录，默认保留旧的 `server/data/skills`。该根目录不得与公共文件或CLI私有根互相包含，避免重复计费和命名空间重叠。隔离API夹具明确配置独立技能根，不读取或写入开发技能。

节点库存计入整个技能根，包括个人、组织、残留解压目录和备份。个人库存只计入 `users/<user_id>`，不将其他用户或组织文件算给这个用户。扫描共用已有条目上限、期限与失败关闭规则。

创建、覆盖和安装先验证输入及峰值字节，再取得节点持久预留；预留覆盖真实写入、解压及清理，并检查技能根所在卷的实际剩余空间。个人写入还在既有SQL账户锁内测量库存、检查个人限额并完成写入；SQLite和PostgreSQL的同机双API竞争均已验证。异常和取消等待实际I/O收尾后释放锁与预留。

额度冲突继承自 `ValueError`，首次API验收发现技能路由的通用参数错误处理会掩盖结构化额度冲突。现显式保留冲突，由全局处理器返回409及 `node_storage_full`。普通无效技能包仍返回原参数错误。

## 当前源码的实际验收

| 类别 | 证据 | 结果 |
|---|---|---|
| 真实文件、节点预留、子进程退出与跨进程竞争 | `tmp/skill-storage-service-sk__i3g1/result.json` | 10项通过 |
| ZIP兼容、本地真实I/O及明确故障输入 | `tmp/skill-archive-service-c5e8qr8k/result.json` | 31项通过 |
| FS/SQLite双API个人限额、组织库存、节点拒绝、用户隔离及预留排空 | `tmp/skill-storage-api-tyskz_4p/capacity-result.json` | 6组通过 |
| S3/PostgreSQL双API及同样的实际技能文件准入 | `tmp/skill-storage-api-z6vk30bz/capacity-result.json` | 6组通过 |
| FS/SQLite原ZIP API及真实压缩/解压线程取消 | `tmp/skill-archive-api-pvf79m7p/result-acceptance.json` | 10项通过 |
| FS/PostgreSQL原ZIP API及真实压缩/解压线程取消 | `tmp/skill-archive-api-lz1b290k/result-acceptance.json` | 10项通过 |

模型调用0，模拟LLM调用0。技能内容在真实文件和HTTP中传输；没有生成模型回复或执行Agent回合。S3组合使用实际自建SeaweedFS 4.52，公共算例库存来自S3，技能仍位于明确配置的本地根，不能称为技能对象存储或物理多主机验收。

服务检查中的I/O观察器调用原操作；故障包括明确降低隔离限额、真实子进程 `os._exit(17)`、保留部分写入及真实第二进程预留竞争。子进程退出后只回收其预留，已落盘的96KiB残留仍计入库存，不以进程死亡推断文件已删除。ZIP恶意元数据、阈值及实际I/O取消沿用前一单元的分类，不能当作生产自然故障发生率。

首次失败报告 `tmp/skill-storage-api-7da6lrud/capacity-result.json` 原样保留：前三组通过，随后额度响应断言失败，未保存当时精确HTTP响应；掩盖路径由源码确认。修复后的两种实际组合均保存了创建和安装的409/错误码回执。此次没有伪造旧响应，也没有删除失败报告。

两轮限额验收的4个实际API出生身份已退出；两轮ZIP兼容API也已退出。两个独立临时PostgreSQL数据库均已删除，自建S3已停止；开发数据库未变更。审查登记见 `tmp/skill-storage-capacity-review-v3-20261109.json`，包含测试辅助代码与源码摘要。

## 原范围内仍需完成

组织没有明确的用户计费所有者字段；本单元将组织技能计入节点，不猜测个人付款人，也不声称已实现组织订阅总额度。全局/跨节点归属继续由原预算任务承担。

个人创建/覆盖沿用原文件写入方式，没有新增多文件崩溃事务；安装沿用既有目录切换及回滚。崩溃残留计费不等于未知历史清理已完成。跨物理主机、不同技能根、根目录迁移及旧实现维护均未在本单元交付。

实际目标卷的剩余空间检查不代替操作系统硬额度。RB-12至RB-17的其余副本峰值、完整生命周期/期限、队列及内存、历史维护、全局并发和WSL/工作目录外边界继续保留，不因本单元缩减。

最终源码摘要、全部Python语法校验、API退出及基础设施清理汇总见 `tmp/mainline-N4-05-skill-storage-capacity-evidence/validation.json`。源码包为 `platform/backups/mainline-N4-05-skill-storage-capacity-20261109.zip`。
