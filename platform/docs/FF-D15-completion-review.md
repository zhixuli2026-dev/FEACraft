# FF-D15：ParaView 离线兼容整项验收

2026-11-18，按原审计 D15 和全功能交付计划第 5、7 节逐条核对。D15 的原始契约是实际 `pvpython/pvbatch` 经公开 `/paraview:run` 执行用户脚本、保存日志和文件；原备注明确将通用 server-manager 属性 UI 分配给其他 D 项。此处不增加或删除原功能，也不以 D15 完成代表整个 ParaView 工作区或全项目交付完成。

## 条件与证据

| 原条件 | 已核验实现与实际证据 |
|---|---|
| 双后端与实际程序 | Windows、Linux 的 ParaView 5.11.2，两个 backend 均实际执行；不是 pip VTK 替代。Linux/Windows 最终各 6 个真实作业，见 [Linux 部署记录](linux-paraview-deployment.md)。 |
| 正常路径与公开 API | 认证后的脚本提交、Job 查询、日志、文件下载、幂等重放；Sphere server-manager、输入 VTP 读取/变换、NACA CFD 处理与图片输出均有实际产物。 |
| 专业正确性、渲染与属性 | Sphere 点/单元/半径；CFD 压力倍率 1、2 逐点回算及 Mach=1 等值线；Linux/Windows 字段比较一致。新验收另用 522242 点、1044480 单元球面独立计算每点半径，最大误差小于 2e-6，实际查看 640×480 PNG。 |
| 权限和数据边界 | 新建独立账户、跨用户提交和 Job 查询 404；非法 backend、越界路径、非 `.py`、缺失脚本、超过 1 MiB 的脚本分别拒绝。历史双库检查覆盖私有配置/收据损坏、输入和目标变化、删除冲突，均不能覆盖原文件。 |
| 异常与日志 | 实际 ParaView 异常返回失败并保留旧成果；仅输出日志的脚本不发布旧文件。输出日志目前在工具结束后汇总，不承诺子工具 stdout 实时中继。 |
| 取消与资源回收 | 新验收读取真实 ParaView 写出的运行目录就绪文件后取消，Job 为 cancelled，原生根和工具子进程停止证明均 verified/scope_empty，旧成果摘要不变。历史真实检查另覆盖脱离子进程、超时、API 退出，不把父进程退出当作整树停止。 |
| 恢复和兼容 | 历史 SQLite/PostgreSQL 各 20 个场景包含发布阶段 API 退出后续交同一作业，不重复执行脚本；当前 Linux/PostgreSQL/S3 实际重启 API 后，两个成功作业及全部 6 份成果的 SHA-256 保持。原 API 参数和返回 Job/backend 保留。 |
| T06 并发与较大数据 | 两个不同算例的 pvpython/pvbatch 原生执行区间实际重叠；每份 VTP 为 45960682 字节，点数约为前一 CFD 验收的 100 倍。属于明确规模的实测，不宣称无限数据或无限并发容量。 |
| 配置、文档、回退 | 固定可选镜像、官方包摘要、Linux PATH 与 Windows 原路径、显式配置及失败策略已有说明。回退镜像并保留数据库/对象存储/原密钥卷；历史发布可明确选取，旧成果不因失败作业覆盖。 |

上述公开接口就是 D15 的调用契约；更完整的可视化 UI、插件、会话协作和 MPI 多节点继续由其原始功能项承担。操作系统硬沙箱、任意外部依赖闭包及跨运行时后代范围继续由 C03/F58/J16 等原任务承担；应用层输入副本不冒充这些能力。全局第 7 节的最终发布门槛仍未完成，主线 4 项和其余功能继续有效。

## 本次新增与失败保留

[较大数据与权限证据](../../tmp/paraview-full-1141c50029e5/result.json)的前两组检查通过，包括两真实成功作业 165、166；该轮整体为失败，原因是取消驱动等待 stdout 就绪标记，而当前 worker 在子进程结束后才汇总输出。实际等待脚本最终报错，作业 167 的原始结果保留，没有改写成取消成功。

[补充取消和重启证据](../../tmp/paraview-cancel-restart-b5c76b1238b1/result.json)通过。它先核对已有成果摘要，只补跑真实作业 168，依据运行目录中的实际就绪文件取消；没有重跑大数据计算，没有伪造日志或停止证明。原生 ParaView 收到 SIGTERM 的日志与取消状态一致。

本次是实际工具和基础设施验收，真实模型 0、模拟 LLM 0。此前真实 DeepSeek 驱动工程链只作为额外证据，不在此重复计数。

## 证据集合和整项结论

- [整项审查汇总](../../tmp/FF-D15-completion-evidence/validation.json)
- [双库 20 场景](../../tmp/paraview-publication-evidence-haq07k6s/result.json)、[对应测试源](../server/paraview_publication_smoke.py)
- [实际工具与脱离后代停止](../../tmp/library-scope-paraview-wk_ywrv6/library-result.json)
- [Linux/Windows 双后端 CFD 矩阵](../../tmp/mainline-linux-paraview-evidence/validation.json)
- [新较大数据与并发结果](../../tmp/paraview-full-1141c50029e5/result.json)、[取消后的作业详情](../../tmp/paraview-cancel-restart-b5c76b1238b1/cancelled-job.json)

当前五份 ParaView 生产模块与已验收的 Linux/Windows 最终审查摘要一致。本轮通过现部署补齐权限、规模、并发、取消和重启的证据，不以旧 ✅ 标签自动关闭。

结论：FF-D15 的原始离线双后端契约及适用的整项交付条件已满足，可记为完成。全功能由 **2/295** 更新为 **3/295，剩 292 项**；主线仍 **49/53，剩 4 项**，资源预算仍 **11/17，剩 6 项**。FF-F54 尚未关闭，继续核对真实 WSL 发现与相关配置条件。
