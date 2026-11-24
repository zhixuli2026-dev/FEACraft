# 同一工作流的 Windows/Linux 执行主机

2026-11-16，N4-07 阶段实现。主线仍为 48/53，剩 5 项；295 项全功能交付目标保持。

## 使用方式

同一 API 的 `target=report` 工作流可以由不同执行主机完成：几何与 Python 后处理放到 Linux；现有 WSL 网格/求解和 Windows Pandoc 留在 Windows。B/C/D/E 确认仍由用户明确提交。每一步的原始输入、版本引用和所选主机在派发时保存，worker 按已有主机队列消费。

`VIBEFLOW_JOB_EXECUTION_HOSTS` 保留按运行时选择主机的默认规则；新增 `VIBEFLOW_JOB_HANDLER_EXECUTION_HOSTS` 对已登记的处理器作明确覆盖。例如：

```json
{
  "windows": {"host_id": "<Linux 的 64 位十六进制 host_id>", "platform": "linux"},
  "wsl": {"host_id": "<Windows 的 64 位十六进制 host_id>", "platform": "windows"}
}
```

上面是运行时映射。处理器覆盖配置为：

```json
{
  "report.build": {"host_id": "<Windows 的 64 位十六进制 host_id>", "platform": "windows"}
}
```

两个 worker 均设置 `VIBEFLOW_WORKER_HOST_QUEUE=true`；跨主机共用 PostgreSQL、Redis 和 S3，并使用兼容的应用版本。占位 host_id 必须换成实际主机身份。历史运行时名称 `windows` 在此表示宿主原生执行；Linux 上的可移植 Python 程序也沿用该值，不代表执行的是 Windows 二进制。

不配置覆盖时保持原有默认选择。覆盖只适用于已登记并通过原请求绑定检查的处理器；未知名称、非法主机、将 WSL 分配给 Linux、未使用 Redis 或跨主机缺少共享存储/数据库均被拒绝。已有派发意图不因部署配置变化被重写。

## 实现与功能范围

`postprocess.run` 复用已有的配置 Python、本地主程序路径解析和源码摘要核验，原封存 execution_spec 保留提交端路径。实际执行事件记录选择的主机、解释器及脚本；输入和发布版本不因此改写。

处理器主机覆盖沿用现有 `vibeflow.job-delivery.v2` 意图和主机队列，没有增加新的队列协议。Pandoc 使用 Windows 侧原已配置的路径与二进制摘要，不把 Windows 可执行文件换成未经登记的 Linux 工具。

验收入口 `live_llm_remote_confirmed_postprocess_acceptance.py` 运行实际几何导出、blockMesh/checkMesh、laplacianFoam、VTK 提取、报告生成和 Pandoc 编译。完整流水线目标是 report；另检查两个报告草稿渠道。80 单元温度对照解析解 `T(x)=300+100*x`，原误差限为 1e-6；提取平均值与报告数值保持一致。D 未确认时后处理不能提前开始，E 确认后报告步骤派发到 Windows。最终 HTML/DOCX 绑定 Linux 后处理发布，后处理绑定原确认求解发布。

报告确定性生成和实际模型验收分开：真实命令对下载产物计算证明，再由官方 DeepSeek `deepseek-flash` 使用文件工具读回。没有模拟模型回复、伪造发布或以合成数据替代求解。

## 验收记录与剩余范围

首轮 `tmp/remote-worker-gynf_2tb/` 通过了实际确认后处理及两份草稿、HTML/DOCX，共 13 个实际任务、2 次真实模型 HTTP 200。该轮使用第二个 Windows 提交 API 编译报告，仅作为前置证据；随后实现处理器主机覆盖，使一个 API 的单条工作流直接完成 report。首轮 37 个原进程身份（含 8 个原生子执行）已独立核验退出，PG/Redis/S3 已清理。源文件副本保留在 `tmp/confirmed-postprocess-v1-source/`。

单 API 的完整链证据为 `tmp/remote-worker-p6639t2k/`：同一 report 工作流所有步骤 succeeded，9 个工程/报告/证明任务及 4 个远端基线任务通过，2 次真实 DeepSeek HTTP 200。原 SQL 封存请求逐字保留，9 条持久派发主机与实际执行身份一致；36 个原进程身份（含 7 个 Pandoc 子执行）已独立核验退出。临时数据库 `tmp/remote-worker-postgres-8efcb4dd762a4b9fbf4b14c5640f1887/` 已删除、访问规则恢复，Redis/S3 已停止。

完整链在 v2 审查源码上执行。随后只修正了一处配置声明条件：仅配置处理器主机、未配置运行时默认时，也生成可移植执行声明。使用上述实际封存请求复现修复前缺声明，再验证修复后声明与已经在 Linux 成功执行的声明完全相同；原运行时映射分支不变。该末次条件修改没有再次重复整条真实链，v3 审查与配置对比证据单列，不冒称又执行了一轮模型或基础设施验收。

最终验收、7 项纯配置/声明检查、清理和源码归档由 `tmp/mainline-N4-07-confirmed-postprocess-evidence/validation.json` 记录。本轮两次完整运行共 4 次真实模型调用；首轮和单 API 轮各 2 次，模拟均为 0。

本实现支持按现有工具环境分配任务，不声称所有外部工具都可原样搬到任意操作系统。其他原生工具环境、专业 child 缺失登记、namespace 尚未保存等失联窗口及其余原计划故障仍需完成；N4-05/N4-07/N7-01/N7-05/N7-06 未由本轮自动关闭。按完整计划继续，不增设权限层或新的阶段退出条件。
