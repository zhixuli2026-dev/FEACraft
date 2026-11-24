# 原 S3 UDF 409：原始数据与缓存清理取证

2026-11-09，N3-07。此页区分原始事实、实际复现与推断；当前仍未把原409或 N3-07 标记完成。

## 找回的原始材料

原调用日志为 `tmp/mainline-N2-s3-professional-handlers-evidence/s3-professional-openfoam-fourth.log`。此前按临时 PostgreSQL 已删除推断原数据库不可用不准确；原读取 API 的实际配置指向仍存在的 SQLite：`tmp/s3-typed-publication-3pbk6e4r/test.sqlite`，以 `mode=ro` 打开。SHA256 为 `fb005cbcac13ecf631bb2cc4556ba3959e6c6a3a0d781e8f2d4569603cb8c0d7`。

Case10 的成功 Job16 发布 `ef601298-a1e9-41bc-9621-5cc285889c1d`。原 SQL 记录的清单摘要为 `ef6a76de9aa8df3a188fb85cbf13682bd86a2d471ce78fcaf5a8f979cf2e0a1a`，与原读取 API 缓存的真实清单一致。Job16 在 05:09:47.49 UTC 成功，05:09:54.82 完成可见性结算；Job17 在 05:09:58.50 创建，05:10:01.87 领取，05:10:03.96 以 `input_stale` 失败、从未启动原生进程。其 detail 和输出为空，没有保存原底层异常。

原对象服务目录为 `tmp/s3-attempt-store-2ejgo90f`。实际核对旧 owner 已退出后，将50个数据文件、12,895,789字节复制到独立目录，以真实 SeaweedFS 4.52 在随机端口读取副本。原 bucket、原清单及70个相关公共文件均可读；原数据逐文件摘要保持。两个所属进程范围已核验停止。证据为 `tmp/original-UDF409-objects-vbu8m2bj/result.json`；原 SQL/清单/时序绑定见 `tmp/original-UDF409-SQLite-evidence-20261109.json`。未迁移或写入原数据库和原对象数据。

## 原目录与实际 Windows 故障

原读取缓存 `tmp/s3-typed-publication-3twha7h4/files/.case-read-views/read-j39330kn` 仍保留。创建于05:10:02.473 UTC，最后删除修改于05:10:03.919，处在 Job17 的失败窗口内。目录已有大量文件被删，留下空 D-post、E-report/Ref/UDF及15个文件。

从原归档 `mainline-N2-s3-professional-handlers-20261105.zip` 取出真实 `restore_source` 和 `remove_owned` 函数。原归档 worker/native 摘要与原发布凭据一致；归档生成时间晚于故障六分钟，因此不声称已找回原进程每个模块的运行时摘要。

对实际原 S3 字节执行原来源投影，可逐项还原原清单的37份输入和目录。对新的数据副本，以 `CreateFileW` 的 `GENERIC_READ` 打开 D-post 且不授予删除共享，再调用原清理函数，真实产生 WinError32。所留15个文件的名称、内容摘要及空 D-post 结构与原目录一致。关闭实际句柄后，同一原函数清理成功。原 SQLite 和原残留目录保持不变。证据为 `tmp/original-UDF409-cleanup-lock-6lls7ee8/result.json`。

首次仅请求属性访问的试验未形成所需锁，保留失败入口和目录 `tmp/original-UDF409-cleanup-lock-u8ban2ca`；属性访问与共享模式的关系见 [Microsoft CreateFile 文档](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilea)。另一轮复制曾因 WSL 映射身份不能复制 Windows 元数据而失败，修正为仅复制并核验字节，保留 `tmp/original-UDF409-objects-5oc0kl3j` 及失败脚本；其清理凭据未保存，不补造停止证明。成功副本有独立完整停止证明。

## 结论边界与下一步

已证明原逻辑文件可还原、原缓存清理未完成，以及真实共享锁可逐字节复现相同残留结构。这为“临时读取缓存删除失败导致来源检查返回通用409”提供了较强因果类别证据。原异常栈、错误码及持锁进程没有保存，不能称原错误码32或某个外部程序已被直接观察，也不能排除清理异常覆盖了更早异常。

取证时 `Cache.cleanup()` 仍递归删除整个容器，存在元数据先删的顺序风险，并将清理失败传播为来源不可校验。后续生产修复已保证写者排空、原归属绑定、数据先删/凭据最后处理，以及已知共享冲突的真实保留和额度计量；未知内容/身份继续拒绝。旧代码对照和修复版分别在原始数据副本上得到真实拒绝与成功引用，完整证据见[清理修复](private-cache-cleanup-sharing.md)。本机修复前重现时 owner/journal 实际保留，不能把顺序风险当作已观察到的凭据丢失。

现有证据可将可复现的有效异常归为本地读取缓存清理失败，并验证此类错误已修复；原精确错误码、持锁进程及可能被清理异常覆盖的更早异常没有日志，仍不声称已直接观察。N3-07整项另需完成本地批次入口及原条件审计，尚不关闭。
