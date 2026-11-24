# 后处理和报告草稿的执行端环境

2026-11-16，N4-07 阶段实现，整项仍未关闭。

## 实现

沿用已有固定程序登记，为 `postprocess.case`、`report.compose` 和 `report.compose-generated` 解析执行端配置的 Python 与本地 worker 脚本。封存的输入清单、原始 execution_spec、请求散列和发布绑定保持不变；实际执行事件保存选择的解释器、脚本及主机。

本轮覆盖已有 OpenFOAM 求解文件的后处理，以及 `E-report/report.md`、`E-report/report.generated.md` 两个报告草稿输出。没有改动数值提取、报告生成或发布校验规则。报告内容由已有确定性生成器组织，真实 LLM 在验收末尾使用文件工具读取产物证明。

这一轮当时尚未覆盖带工作流确认记录的 `postprocess.run`；后续已完成[同一工作流的 Windows/Linux 执行主机](confirmed-postprocess-workers.md)，从实际几何、网格、求解和 B/C/D/E 确认，到 Linux 后处理和 Windows Pandoc HTML/DOCX。本文旧轮文件来源是实际求解后的文件，不冒称已有确认的 mesh/solve 发布关系。其他外部工具环境和原故障组合仍保留原计划范围。

## 真实验收

显式入口为 `live_llm_local_post_report_acceptance.py` 和 `live_llm_remote_post_report_acceptance.py`，复用已审查的真实模型、隔离 API、数据库和基础设施辅助代码；执行前校验全部登记源码散列。

两个入口均调用实际 OpenFOAM `blockMesh`、`checkMesh`、`laplacianFoam` 生成 80 个单元的导热结果，逐单元对照解析解 `T(x)=300+100*x`，误差限为 1e-6。随后实际 VTK 提取平均温度和 19 点曲线，核验 verification 通过且不声称实验 validation，再检查生成 PDF、SVG、PNG 的实际内容。最后生成两个报告草稿，要求已发布、已应用、未过期，报告数值与提取指标完全一致。

本地采用 Windows FS/SQLite，求解前置任务使用现有 WSL 工程命令路径，后处理与报告程序运行在 Windows。远端采用 PostgreSQL/Redis/S3，实际 Linux worker 使用配置的 `/opt/vibeflow-worker-venv/bin/python3` 运行专业程序，Linux worker 本身以 UID 65534 运行。三个专业任务执行前后的原始 SQL 输入清单逐字相同。

实际命令读取求解、提取、图表和报告文件，计算散列证明，再由真实 DeepSeek `deepseek-flash` 读回；提示不提前提供文件证明。远端入口另外验证取消、关闭和重启。

## 证据与边界

本地第二轮证据为 `tmp/local-post-report-uczbt894/`：5 个实际任务、2 次真实模型调用通过。平均温度为 349.9999997764826 K，与解析值 350 K 在原定误差限内一致。

最终源码的本地证据为 `tmp/local-post-report-xuv43u8v/`，5 个实际任务与 2 次真实模型调用通过。远端证据为 `tmp/remote-worker-xfucqs_o/`，5 个求解/提取/报告/证明任务及 4 个队列启动/取消/关闭/重启任务通过，2 次真实模型调用均返回 HTTP 200。最终通过统计为 14 个实际任务、4 次真实模型调用、0 次模拟模型调用。

最终远端数据库与访问规则记录为 `tmp/remote-worker-postgres-03416ec0da8446eab0f464fedcbf629e/`：临时数据库已删除，访问规则已恢复，测试 Redis 与 S3 已停止。独立进程退出记录位于两个最终算例的 `post-report-exits.json`；源码和证据散列由 `tmp/mainline-N4-07-remote-post-report-evidence/validation.json` 归档。

首轮本地 `tmp/local-post-report-4v059jl8/` 与远端 `tmp/remote-worker-3gc5z1ea/` 在首份报告后的检查失败：测试错误地要求包含字面文本 `350`，实际报告保留真实浮点结果。修正仅解析报告表格中的数值，要求其等于实际提取值，并保留原定解析误差限。没有改写报告数值、放宽验收容差或模拟模型返回；两轮均未进入真实模型调用阶段，不计为最终通过。已独立确认这些已记录任务的原始进程身份退出。

第二轮远端 `tmp/remote-worker-0s_b3aq2/` 的功能与 2 次模型调用通过，重启阶段因验收辅助程序读取尚未写完的就绪 JSON 失败。实际进程就绪文件改为同目录临时文件写完后原子替换，保留真实进程身份、UID 和 Redis 检查。此轮整体不计通过；18 个原始进程身份均已核验退出，数据库已删除、访问规则已恢复。对应数据库记录为 `tmp/remote-worker-postgres-ba64347188574090bc9526c835f95a9a/`。第二轮本地与远端的共 4 次真实模型调用单列为中间证据，不混入最终通过统计。

下一阶段优先核对 [原 N4-07 验收条件](next-stage-mainline-plan.md)：关闭与取消区分、完整收尾后确认任务、失联恢复。将已有证据与实际缺口逐条对应，不继续为本阶段添加退出条件；其余专业工具与确认工作流环境仍保留在完整 53/295 交付范围。
