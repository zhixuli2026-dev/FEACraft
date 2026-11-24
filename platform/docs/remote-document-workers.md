# PDF/PPT 专业任务的远端环境映射

2026-11-16，N4-07 阶段实现；整项尚未关闭，主线仍 48/53。

## 提交和执行

独立专业任务及没有批次投递设置的工作流，现在遵循现有 `distributed_jobs` 设置；已登记批次的明确投递策略继续保留。此前 `workflow_batches.delivery_mode()` 对非批次请求固定返回 local，导致开启远端主机映射后 PDF 提交直接被拒绝。本轮通过实际 API 请求发现并修复此问题。

明确配置执行主机时，`pdf.number` 和 `ppt.build` 的原 handler 记录增加 `vibeflow.handler-environment.v1` 声明，保存逻辑程序名、原始 argv 前缀、提交主机和程序源码 SHA-256。封存的原始 execution_spec、请求散列、输入和发布 descriptor 不改写。

执行端先核验声明与封存请求一致、程序属于固定登记表且本地源码散列一致，再使用执行端服务 Python 和本地脚本路径运行；相对输入、输出、execution ID 等其余参数逐字保留。原 handler 校验也针对这个已核验的本地路径投影进行，原始 execution_sha256 仍校验原提交内容。实际选择通过 `attempt.execution_environment` 记录，包含执行主机、解析后的 argv 和 Python 版本。

没有配置主机映射时，两种 handler 保持原路径与原记录格式。没有添加任意路径替换规则，也没有改写用户 shell 命令。本阶段最初验证 PDF 页码与 PPTX worker；后续增加的几何与 FloTHERM IR 转换见 [remote-geometry-workers.md](remote-geometry-workers.md)。其他专业 handler 的解释器、求解器和 WSL 环境按后续原计划接入，不能把本轮结果泛化为全部专业工具已支持跨平台。

## 实际验收

远端显式入口 `platform/server/live_llm_remote_documents_acceptance.py`：Windows API 提交、实际 Linux worker 执行，使用共享 PostgreSQL/S3、Redis 和独立 Linux 算例缓存。

- 真实三页 PDF 由 Linux 程序添加页码；下载后逐页检查页码与页数，源 PDF 字节不变。
- 真实 PPTX 包含可编辑文字和原始 PNG 图片；下载后通过 python-pptx 检查页面、文字与嵌入图片字节。
- 两次执行前后的原始 input_manifest_json 相同，仍保留 Windows 提交路径；环境事件证明实际使用 `/opt/vibeflow-worker-venv/bin/python` 和 Linux 下的脚本路径，原始其余参数不变，真实进程身份为 Linux。
- 后续真实 Linux 命令对两个已发布文档计算 SHA-256 并写入证明文件；DeepSeek `deepseek-flash` 真实调用工具读回该证明。模型没有预先获得文件内容。
- 同一入口继续覆盖远端取消、关闭和重启。

默认本地入口 `platform/server/live_llm_local_documents_acceptance.py` 在 Windows、FS/SQLite 下运行相同两类真实文档功能，确认未增加远端环境声明；随后真实任务计算产物证明，真实 DeepSeek 读回。

最终远端证据在 `tmp/remote-worker-pl7ncp6p/`，共 7 个任务、2 次真实模型调用；数据库与访问规则清理在 `tmp/remote-worker-postgres-e08c8da408a14fa581d781a29c9a9aab/`。最终本地证据在 `tmp/local-documents-3r87h4ep/`，共 3 个任务、2 次真实模型调用。最终通过情况、独立进程退出观察和来源散列由本阶段 `validation.json` 记录。

失败尝试保留：`remote-worker-_zz32s_b` 暴露专业任务被固定投递到 local；`local-documents-immbv9o7` 已通过两个文档功能，但证明文件使用 Windows CRLF，而验收器默认期待 LF，模型调用前即被拒绝。后者改为使用已有 expected_content 参数核验实际原文件字节，没有修改文件或伪造模型结果。这两次均不计为最终通过。

## 后续

下一步将同一已登记程序映射用于其余可移植 Python handler，并处理需要外部原生工具的明确环境选择及相应子执行故障验收。N4-07 原始剩余范围及 295 项全功能目标保持不变，不新增权限或隔离层。
