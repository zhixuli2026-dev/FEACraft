# 同算例串行批次

## 2026-11-18 多工况并发与重名目录

V04 复验使用 PostgreSQL、文件存储、两个独立 Redis worker 和真实 icoFoam。作业 5、6 同时运行，各自写入所属工况的 400 单元速度场，各有独立的作业日志、执行凭据和发布 ID。单独修改第一个工况的初始场时，只有第一个发布失效。两个目录显式重跑均产生新发布，保留旧版本，原生跟踪记录私有副本内的旧文件清理。重启 API 后，使用原幂等键取得原批次，两名 worker 均已退出。[本次求解记录](../../tmp/live-cli-startup-9ycu6hzr/same-case-serial-batch.json) · [worker 退出记录](../../tmp/live-cli-startup-9ycu6hzr/same-case-serial-worker-stops.json) · [此前同场景 S3 求解记录](../../tmp/live-cli-startup-a92jksj1/same-case-serial-batch.json)

V05 复验通过公开派生 API 同时将两组内容不同的输入写向同一目录。FS 由左侧请求成功，S3 由右侧成功，另一请求均收到目录已存在的冲突。逐文件回读确认四个目标文件全部属于获胜方，两个来源的输入和结果完整。每个后端只提交一条文件树更新事件，作业列表不变，事件流中没有发布指针变更。实际求解写入同一目录时的显式重跑和版本保留见上面的 V04 记录。[FS 冲突记录](../../tmp/live-cli-startup-u6kfwwqj/derive-collision.json) · [S3 冲突记录](../../tmp/live-cli-startup-xzb2cule/derive-collision.json)

本次修正了派生输入选择。工况根目录的 `log.*`、`postProcessing/` 和合并存储的并行结果目录被排除，初始场和 `system/log.config` 保留。文件系统的目录重命名现在也会提交文件树刷新事件。FF-C11 仍需验收派生中途进程退出及大目录等恢复边界。

首次复验因几何预览排队时尚未启动 worker，300 秒后超时，模型调用为 0。将两个真实 worker 提前启动后通过。本阶段五次成功验收各产生两次真实 DeepSeek 请求，用于工具读取及回复，共 10 次。求解和存储竞争均由实际基础设施执行。[首次失败](../../tmp/live-cli-startup-rln9en3b/live-result.json) · [求解回读](../../tmp/live-cli-startup-9ycu6hzr/live-result.json) · [首次 FS](../../tmp/live-cli-startup-lfx9el4g/live-result.json) · [首次 S3](../../tmp/live-cli-startup-e5knuwwx/live-result.json) · [最终 FS](../../tmp/live-cli-startup-u6kfwwqj/live-result.json) · [最终 S3](../../tmp/live-cli-startup-xzb2cule/live-result.json)

原 `POST /cases/{case_id}/jobs:batch` 的 `operations` 增加 `kind: "serial"`，成员提供 `run_dir`、`solver` 和原有预算。使用注册处理器 `solve.openfoam.prepared`，复用现有 `solve_worker.py`、实际串行 OpenFOAM 调用、监督、停止证明、场校验和不可变发布。

每个运行目录有独立的 `serial:` 发布通道、日志和求解凭据。准备运行入口不替代要求工程确认及固定网格引用的正式 `solve.openfoam` 工作流；两者共享串行 worker，但各自保留原准入语义。

完整公开算例封存后，worker 验证所有文件摘要。对于明确重跑，配置列出该成员已知的旧数值时刻和串行结果凭据；受监督的真实子进程只在本次私有副本中校验并删除这些文件，删除操作与求解操作均由 strace 实际记录。其他工况和原始输入保持可见并继续校验。删除实时兼容产物与发布新结果仍须匹配准备前摘要，不直接清理用户的实时工作目录。

来源同时包含原有工程输入语义及实际文件查询：跨目录 include、缺失文件和目录枚举参与来源判断。原完整清单保留为 origin_source；私有清理及产物变化有明确记录，不能把任意文件变化当成可忽略的运行输出。来源校验使用现有 FS/S3 机制，固定结果仍由不可变发布提供。

## 验证状态

新入口 `live_llm_same_case_serial_batch_acceptance.py` 已通过隔离 FS/PG 与 S3/PG 命名场景验收，覆盖真实几何/网格、两个 Redis worker、两个 400 单元串行工况、输入变化、共享 include、可选缺失输入、非法成员回滚、同目录明确重跑与实际 API 终止后的幂等查询。同目录明确重跑及独立空缓存重启已通过；完整取消恢复继续验收，不能将命名场景替代整项。

旧 commands、MPI、UDF 及混合 MPI/UDF 请求的规范化内容和摘要已作纯协议对比；不同 kind 的参数互相拒绝。证据为 `tmp/same-case-serial-request-compatibility-20261109.json`，模型调用为 0。

工程场景通过后才调用已授权真实 DeepSeek 作独立结果证据回读，不模拟模型回复、流式输出、工具选择或原生求解结果。全部 53 项主线、295 项全功能范围继续有效，本入口不等于 N3-07 整项关闭。

## 当前实际证据

2026-11-09（同case串行批次）：注册solve.openfoam.prepared复用原串行worker，FS/PG与S3/PG双Redis worker、两个400单元工况、输入隔离/共享include/缺失文件/未读文件/原子回滚通过；明确同目录重跑包含完整原封存、实际跟踪私有旧结果清理和新版本，重启保持原批次/幂等。原正式串行/MPI/下游链兼容通过。新增6次真实DeepSeek、25828tokens，模型仅独立回读，模拟0。两次测试取数失败保留。主线36/53、剩17，完整取消恢复/worker猝死/原409继续，N3-07仍未关闭。见[实现与证据](same-case-serial-batches.md)。

本地证据tmp/live-cli-startup-jgcijtqi，S3证据tmp/live-cli-startup-a92jksj1；共8次批次成员串行求解。兼容证据tmp/live-cli-startup-80iereuh。首次四次原生作业均成功，测试从4000字符截断日志找不到开头清理标识；完整约74KB日志和35文件实际删除跟踪已核验。下一次仍是四次原生作业成功，测试误从批次摘要读取完整attempt.outputs；已改为保留原完整作业详情中的ID，原断言均继续执行。两次失败均模拟0、真实模型0，不作为完整验收通过。

5个临时PG已删除，1个S3及4个Redis已停止，8个worker出生身份退出、13个API端口无残留已核验。源码包及逐文件/全部Python AST校验见tmp/mainline-N3-07-same-case-serial-evidence/validation.json。
