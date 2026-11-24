# 执行输出补传恢复：N5-04第一段

2026-11-10（北京时间）。主线仍为37/53完成、4进行中、12待开始，剩16项；295项全功能目标保持。本段交付输出归档恢复，N5-04尚未完成。

原生程序已经实际退出、输出清单已形成，但API在S3上传途中退出时，现在可以显式补传同一Job/attempt的缺失对象，复核已有对象的完整字节，并持久保存唯一恢复回执。不再次启动原生程序。补传返回`archived`及`finalization_pending:true`，不宣告Job成功；后续专业校验、完整内容排空、发布及outbox仍需接续。

## 实现与边界

- 原worker在上传前持久保存`output_preparation`和`attempt.output_preparation_ready`，绑定输入、清单、case、worker、根进程和已登记子进程、实际停止回执及累计输出用量。没有当前预算或完整实际停止证明时，不产生该证明。它不是全部日志和专业收尾已经排空的证明。
- `GET /api/v1/jobs/{job_id}/output-recovery`检查可恢复状态；`POST /api/v1/jobs/{job_id}:recover-outputs`要求原attempt及清单摘要。每次重新检查活动账户、case写权限、回收站、当前attempt、存储绑定、原停止证明及持久历史；陈旧请求拒绝。
- 仅在实际观察到原worker的确切出生身份退出后补传。正向退出观察有256项内存上限，并在写操作时保存绑定的`attempt.output_owner_exited`证书供其他API/重启读取；不缓存存活、身份不匹配或查询不可用。已有正向观察不会因为后来PID复用或探测失败被推翻。未曾取得正向观察的其他PID复用情形仍可能拒绝，未声称全部解决。
- 复用既有输出容量预留和条件不可变写入；远端已有文件逐字节摘要核验，仅缺失成员从原本地副本补传，最后核验完整清单。缺失或损坏的唯一副本不自动重算，也不凭远端前缀存在判成功。
- 取消必须等待实际I/O线程返回，之后才可写`attempt.output_copy_drained`。该凭据绑定确切预留、写者、输入、清单及大小，使同一仍存活API后续请求能够重试；仍保留原完整额度，不提前释放未知写入。期限后的元数据结算使用有限的`settlement`锁预算，是既有六个入口之外新增的第七个释放/排空入口，不授予新写入额度。
- 并发API读回恢复回执及attempt采用同一SQL查询快照。若对方已完成同一清单提交，重新核验远端并返回相同回执。V2恢复回执绑定原owner退出证书；V1历史读取兼容保留。
- 每个API最多2个本入口操作，等待入场0.1秒，操作绝对预算300秒。这不是全局多worker额度/队列已完整收口。缺失文件上传仍沿用完整`read_bytes`，未完成全局内存硬边界，RB-14继续。

修改`attempt_workspace.py`、`jobrunner.py`、`output_archive_storage.py`、`routers/jobs.py`，新增`services/attempt_output_recovery.py`及显式验收入口`attempt_output_recovery_acceptance.py`。正常完成仍沿用原完整收尾路径。

## 实际验收

本段真实模型调用0、模拟LLM调用0。实际安装的Python通过生产原生进程归属/启动门执行，写入262144字节随机文件并在外部计数器追加一次调用。真实HTTP、SQLite/临时PostgreSQL及自建SeaweedFS 4.52执行；没有模型回复、流、工具选择、Agent回合或CLI推理替身。不得将这些基础设施结果当作真实模型验收。

故障注入转发原S3写入后才暂停实际线程；终止API使用`os._exit(79)`。元数据边界转发原事件写入及原SQL提交，在实际提交前/后暂停。取消通过三次真实`Task.cancel()`检验线程未被遗弃。删除缓存仅作用于已验证完整归档、路径位于隔离根内的确切测试run目录。

| 隔离证据目录（均位于`tmp/`） | 实际结果与范围 |
| --- | --- |
| `attempt-output-recovery-wrvsmide` | 旧源码反例成立：内存有成功停止证明，上传途中实际退出后SQL没有该证明，输出pending且恢复GET为404；2条反例检查通过。 |
| `attempt-output-recovery-awtuoor3` | 第一版基础补传/同回执重读通过；不是最终版本全部验收。 |
| `attempt-output-recovery-0b_4tudy` | 失败保留：实际隐藏缺失源文件后拒绝正确，恢复源文件后同API仍被自己的写预留挡住。 |
| `attempt-output-recovery-j4p7epam` | 失败保留：排空审计字段`token_sha256`触发现有凭据字段限制，未成功写入；改为`claim_sha256`，未放松限制。 |
| `attempt-output-recovery-lp7mzgyi` | 实际隐藏/恢复源文件，同API重试通过。 |
| `attempt-output-recovery-c1ewkx6x` | 失败保留：GET曾看到实际退出，后续POST归属探测拒绝；旧版未记录该次底层原因，不能反推为某个具体PID复用或查询错误。 |
| `attempt-output-recovery-cf7xdx7f` | 先行PG取消/双API通过，但一次并发响应为普通409，需显式重试；最终版已加强同回执收敛。 |
| `attempt-output-recovery-ptrl56a3` | 最终SQLite：实际阻塞I/O的三次取消、线程排空前对端busy、排空后双API同时200且同一回执通过。 |
| `attempt-output-recovery-jneec8vf` | 最终PG：源文件缺失/修复及上述取消/双API收敛全部通过。 |
| `attempt-output-recovery-7w4_3yas` | 最终SQLite：完整远端归档后实际删除本地run，恢复元数据提交前退出；重启回滚未提交事件，产生新唯一回执。 |
| `attempt-output-recovery-8i803i4v` | 最终PG：完整远端归档后实际删除本地run，恢复元数据提交后退出；重启保持原唯一回执。 |
| `attempt-output-normal-rh9ew84r` | 最终SQLite无故障兼容：真实原生作业成功、原完整排空检查点和停止证明成立、归档额度物化，没有恢复事件。 |
| `attempt-output-normal-wvrgys4y` | 最终PG无故障兼容，同上。 |

每份证据为目录内`output-result.json`。最终六份场景对应相同生产源码；验收入口随v8/v9/v10新增场景，不能将早期入口摘要宣称与当前相同。当前导入审查为`tmp/N5-output-resume-review-v10-20261110.json`，此前版本及失败原件保留。另有一次首轮夹具在对象存储进入前读取settings失败，未启动S3/API，不计入13轮基础设施执行。

13轮共13次实际原生程序调用；每轮外部计数均为1。共13次明确API退出故障，30个观察到的API出生身份有实际退出记录，17个自有API端口另行复核。13份自建S3停止记录和4份临时PG删除记录保留；checkpoint再次只读查询4个确切临时数据库不存在，不写开发库。最终两轮取消共6次实际取消请求；最终四轮恢复各含陈旧attempt/清单的两条拒绝检查，共8条协议负向检查，分别记账。

## 原N5-04要求的兑现与后续

原四条`remaining`原文完整保留在主线台账，不能删去已知未交付部分：

1. 输出已实际停止并形成早期清单后的归档补传已交付；证据生成/同步/专业校验中断、尚未形成完整证明的恢复，以及旧无证明记录的诊断与人工处理继续。
2. 原`native_execution_smoke`两处LaTeX物理停止证明缺失继续明确处理，不能补造排空。
3. 本段完成上传前准备/停止证明、上传重试及归档SQL提交中断；未完成完整Job终态、不可变发布和outbox提交中断恢复。
4. 已完成远端完整时丢失本地run后归档元数据恢复；专业缓存重建、重建中断、旧staging/恢复目录治理及证据/校验前收尾继续。

下一段先连接已归档的早期输出与原完整收尾：校验并恢复累计输出预算、受限授权专业finalizer子进程、复核原输入/处理器/工具链、恢复受额度约束的执行缓存与日志、再形成完整排空证明，接入原不可变发布及outbox。不得更换原worker历史、重启solver或以原生exit0代替专业成功。基础实现及真实故障证明完成后再进入对应专业处理器矩阵。

源码包：`platform/backups/mainline-N5-04-output-upload-recovery-20261110.zip`；清单/全部Python AST、源码摘要、实际证据、进程/临时库复核见`tmp/mainline-N5-04-output-upload-recovery-evidence/validation.json`。该归档是源码和验收证据索引，不包含密钥或用户数据。
