# 报告、工程成果与状态读取的恢复额度

2026-11-06，本批将 25 处纯读取协调范围接入 `case_read_capacity.reading`。S3 中这些读取可能先恢复文件事务或更新写入栅栏，因此也会产生远端对象和版本；它们现在在进入算例协调器之前取得工作区所有者额度，返回完整普通数据或响应对象后结束读事务，再结算额度。成功、缺失资源、选择冲突等异常都遵循相同收尾规则。本地文件后端继续原协调语义。

## 覆盖范围

| 文件 | 本批协调范围 |
| --- | ---: |
| `routers/reports.py`：草稿、派生报告、工程报告、LaTeX 的结果、历史、产物与源码 | 13 |
| `routers/geometry.py`：scene/validation 共用读取和发布历史 | 2 |
| `routers/mesh.py`：结果和发布历史 | 2 |
| `routers/solve.py`：结果和发布历史 | 2 |
| `routers/postprocess.py`：结果和发布历史 | 2 |
| `routers/workspaces.py`：事件和溯源 | 2 |
| `routers/jobs.py`：批次详情和列表 | 2 |

合计 25 处协调范围，对应本次 28 种 GET/HEAD 请求。读取期间先将 ORM 数据转换为普通结果；没有把读事务回滚后失效的 ORM 对象传给后续业务。

特意保留的混合路径是后处理启动前的读取、批次取消前的读取、上传状态与显式准备恢复：它们会继续使用 ORM 状态、启动作业或改变预留，不能机械替换为回滚型读取，需要按各自业务事务和未来资源预留单独接入。这些是剩余范围，不算完成。

## 实际验收

使用隔离真实 SeaweedFS，开启版本保存；SQLite 与临时 PostgreSQL 分别执行同样的接口检查。测试完全不调用 LLM，不包含模拟 provider、模型输出或 CLI 推理。新增入口 `server/publication_read_capacity_checks.py` 分为 `--group publications` 和 `--group status`，数据库可用 `--database postgresql` 指定隔离临时库。

| 组 | SQLite | PostgreSQL |
| --- | --- | --- |
| 成果读取与实际 Pandoc | [54 条检查记录](../../tmp/publication-read-capacity-9298ybk2/checks.json) | [54 条检查记录](../../tmp/publication-read-capacity-7xdyjya3/checks.json) |
| 事件、溯源与实际命令批次 | [9 条检查记录](../../tmp/publication-read-capacity-aqqj5x5b/checks.json) | [9 条检查记录](../../tmp/publication-read-capacity-674ae5bo/checks.json) |

每个数据库都验证全部 28 种请求在零存储额度下返回 429、没有增加远端物理用量，且额度预留结算；恢复额度后，空历史返回 200、缺失成果返回 404。还验证 400 错误返回的结算，以及读取实际远端 scene/validation JSON。

正向成果没有使用伪造报告字节：每个数据库实际运行 Pandoc，生成并发布报告，再核对报告结果、HTML 下载、历史查询和未发布产物的 404。修改源 Markdown 后，旧报告标记过期，其已发布 HTML 字节保持不变。状态组各实际执行两个命令作业，核对批次详情/列表中的两个成功状态，验证读会话结束后结果仍可序列化。

126 条检查记录是这些限定场景的证据，不是新增 126 项任务。测试进程均已退出；[成果组 PostgreSQL 清理](../../tmp/pubread-postgres-6ccjoc9x/cleanup.json)与[状态组 PostgreSQL 清理](../../tmp/pubread-postgres-9vzwzjrn/cleanup.json)确认临时库已删除，开发库和开发服务未修改。

## 剩余工作

本次没有重新执行所有几何/网格/求解/LaTeX 编译流程，其成功发布格式依靠已有证据，本批重点是额度、读取事务及返回对象生命周期。并发慢存储、更多恢复故障、共享成员组合及混合读写入口仍需对应验收。纯读取的输出限制也不等于底层扫描与 I/O 已有完整预算。

RB-12 继续处理混合读写、上传未来预留、ZIP/备份/导出峰值；RB-13 至 RB-17 以及全部 295 项功能范围保持。主线仍 33/53 完成、剩余 20；资源块 11/17 完成、剩余 6。本次没有新增模型调用，累计真实模型请求仍为 62 次。
