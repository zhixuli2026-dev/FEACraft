# MPI的S3输入读取与取消恢复验收

2026-11-09。正式MPI运行中取消与明确重算，在FS/PG和S3/PG的本轮命名场景均通过。主线36/53完成、剩17项；N3-07仍进行中，UDF、多工况及原S3 UDF单次409归因继续，295项全功能范围不变。

## 真实计时与修改

先行隔离轮tmp/live-cli-startup-9yzsfq2o在未修改30秒HTTP限制时重现确认C超时。mpi-stage-timings.jsonl记录实际原函数：workflow_MPI.prepare为8.735秒，attempt.seal为15.546秒；进入启动/封存输入复核后，测试失败终止API，未写完对应结束记录。这些未完成span不能当成完整耗时，更不能把进程结束后的墙钟差算作执行耗时。确认求解的同步路径累计了多次整输入读取。

private_cache.Cache.fetch_many保留整视图额度准入，改为最多4个网络读取在途；仅调用线程按原规则写每个文件和先行持久日志。读取或写入失败后停止安排新成员，等待已安排读取全部结束，才允许释放额度或清理缓存。字节/文件数上限、每个文件实际长度及摘要、读前/读后对象版本检查保持。

attempt_objects.ensure_inputs复用既有4路流式verify_members：每次仍读取和校验每个远端封存成员，包括已有本地缓存时；缺缓存时按原独立暂存和额度准入恢复文件，最后才安装本地输入。保留清单/实际成员集合检查、逐字节大小与SHA、原可执行权限和本地快照检查。损坏输入仍分类input_stale，未将冷缓存或旧本地副本当作远端真值。不可变上传的条件创建与账户持久额度日志仍串行。

新增mpi_timing_observer仅在隔离夹具包装实际原函数，记录角色、父子span、耗时和异常类别；不修改返回值、模型回复、原生结果、预算或异常。入口同时保存实际执行的入口与计时代码。取消证明还核对持久worker身份中的实际API PID，选择不同API发出取消，避免把请求实例误当实际执行持有者。

## 完整工程验收

| 组合 | 证据根 | 真实模型回读 |
|---|---|---:|
| FS/PostgreSQL | tmp/live-cli-startup-awspbydr | 2次，8632 tokens |
| S3/PostgreSQL | tmp/live-cli-startup-dkck4kbh | 2次，8742 tokens |

每轮实际写320个额外参考文件，MPI初始工作区359文件、13条实时选择，逐一核验摘要；真实100×100网格中观察两个非root icoFoam rank，由与持有者不同的API取消，根/子执行停止、写入排空和原7个出生身份退出均通过。其他用户被拒绝，第二API重复取消不重放，残缺结果不发布，工作副本及C阶段保持。

实际终止原请求API后，新API查询原取消历史和重复派发均不新增执行；明确重算恢复原400单元基准网格及控制参数，复用几何、重建网格与求解，B/C/D逐级明确确认；两rank真实求解至0.5，400个速度向量有限且符合原界限，正式solve发布保留真实MPI凭据，320参考输入仍在。相同重算key返回同一新流程，旧取消历史不变。

本批共4次api.deepseek.com / deepseek-flash真实HTTP200、17374已报告tokens，模拟0，最终usage全部可读。模型仅独立读取实际证据摘要；真实工程执行与模型规划不可混称。FS、S3、真实PostgreSQL、原生进程及纯协议检查分别留存。

S3原轮失败的同类确认求解请求在本轮初次运行26.265秒、明确重算27.453秒完成；该命名场景未再超时，但距离30秒上限仍近。完整大规模输入响应SLA、资源上限及跨物理主机矩阵继续N4-05/N6-03/N7-04原范围，不能由这一轮通过证明任意规模均满足性能要求，也不代替原S3 UDF409归因。

## 真实基础设施负向检查

tmp/s3-attempt-store-io2hn2ep/input-upload-result.json通过24个真实S3对象的上传回读、相同内容重试、错误原对象保留、真实本地源被改动后拒绝、读取缓存实际生成、错误准入长度拒绝、冷输入恢复、暖缓存复核和实际远端对象被改坏后的拒绝。确认所有已启动读取/上传线程退出；远端坏对象不能被正确本地缓存掩盖。此专项模型0，不属于LLM验收。

两轮先行专项失败保留：lz25xmia新增缓存路径未先隔离本地根，被默认根的链接/重解析点检查拒绝；之后明确隔离本地文件和CLI私有根，不跟随链接、不删除未知目录。wt0z563l发现新流式复核把原input_stale改成普通ValueError；已在封存输入边界恢复原分类，输出归档接口未变。前一阶段七轮工作流和先行专项失败继续保留，见[上一阶段记录](mpi-inputs-cancellation-progress.md)，不改写成通过。

本批3个隔离PG库已删除，5个自建S3服务已停止；开发数据库仅只读获取已授权profile26凭据。未运行历史模拟LLM回归。MPI原生worker/receipt/求解器、持久投递、额度、停止及工作流规划等核心模块与上一归档保持字节相同。

复验使用项目.venv/Scripts/python.exe -X utf8 -B运行live_llm_mpi_cancel_recovery_acceptance.py --source-profile-id 26 --postgres，可加--s3；基础设施入口immutable_input_upload_acceptance.py无模型调用。完整源码包platform/backups/mainline-N3-07-mpi-input-read-fix-20261109.zip，SHA、源码AST及证据审计见tmp/mainline-N3-07-mpi-input-read-fix-evidence/validation.json。下一步UDF构建/集成正式工作流及其实际取消恢复，之后类型多工况与原剩余要求。
