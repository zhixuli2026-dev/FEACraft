# 几何与 FloTHERM 转换的执行端环境

2026-11-16，N4-07 阶段实现。主线仍为 48/53，整项尚未关闭。

## 实现范围

沿用 PDF/PPT 的固定程序登记与执行端路径解析，增加 `geometry.preview`、`geometry.export` 和 `flotherm.convert`。不改写封存的原始请求、输入清单或发布绑定。

几何任务使用执行端现有运行配置中的 `python`，未配置则使用服务解释器；FloTHERM IR 转换使用服务解释器。两种解释器角色分别登记为 `configured_python` 和 `server_python`。执行前核验原始参数前缀、登记程序名与源码散列；只解析解释器和脚本路径，其余参数逐字保留。

`attempt.execution_environment.worker_python_version` 表示 worker 服务自身的 Python 版本。几何载荷所用解释器及 CadQuery 版本另由实际生成的 `A-geo/toolchain.json` 提供，不能把服务版本当作载荷版本。

没有配置跨主机映射时，沿用原有本地执行记录。外部求解器、后处理和报告组合仍按原计划处理，本轮不代表所有专业工具均已支持跨主机执行。

## 验收方式

两个显式入口为 `live_llm_local_geometry_acceptance.py` 与 `live_llm_remote_geometry_acceptance.py`，均要求源码审查散列匹配。保存几何脚本后等待 API 返回的自动预览作业，再导出，避免重复提交预览。

- 实际 CadQuery 构造 2×3×4 长方体，检查预览顶点、面和包围盒。
- 实际导出 STEP，重新导入后验证体积为 24；远端另下载并检查 STL。
- 实际转换包含 5W 热源的 FloTHERM IR，核验生成的覆盖报告。
- 实际命令读取已发布产物并计算散列证明；真实 DeepSeek `deepseek-flash` 调用文件工具读回证明。提示不预先提供证明内容。
- 远端使用实际 PostgreSQL、Redis、S3 与 Linux worker；原始 Windows 请求保持不变，普通载荷使用 UID 65534。继续核验取消、关闭和重启。
- 独立观察原始进程身份退出，并检查临时数据库、访问规则与基础设施清理。

## 当前证据

最终本地证据为 `tmp/local-geometry-njuv5bb7/`：4 个真实任务成功，2 次真实模型调用成功，STEP 读回体积为 23.999999999999996，4 个原始进程身份均已退出。

Linux 已在现有 `/opt/vibeflow-worker-venv` 安装 CadQuery 2.8.0；使用实际非特权用户检查导入成功，`pip check` 返回无依赖冲突。环境记录为 `tmp/N4-07-linux-cadquery-environment-20261116.json`，包含 Python 3.12.3、OCP 7.9.3.1.1、NumPy 2.5.4、VTK 9.6.2 与 SciPy 1.18.1。

最终远端证据为 `tmp/remote-worker-flq7p8bb/`：4 个专业功能及产物证明任务，加上队列启动、取消、关闭、重启等基线任务共 8 个实际任务通过；2 次 DeepSeek 真实调用均返回 HTTP 200。STEP 重新导入体积为 23.999999999999996，FloTHERM 覆盖报告包含 5W 输入热源。三个专业任务的原始输入清单未变；实际执行事件分别记录配置的几何解释器 `python3` 和服务解释器 `python`。

数据库与访问规则清理证据为 `tmp/remote-worker-postgres-67c8db6918d048288db63e0dbbd647b3/`；临时数据库已删除，访问规则已恢复。实际 Redis 和 S3 测试服务均已停止。最终通过合计 12 个真实任务、4 次真实模型调用，模拟模型调用为 0；源码、环境与独立退出观察散列由 `tmp/mainline-N4-07-remote-geometry-evidence/validation.json` 归档。

中间记录保留：`tmp/local-geometry-scas8dz8/` 为早期源码的通过结果，包含 2 次真实模型调用，不替代最终源码验收。`tmp/local-geometry-3l820n7r/` 未通过，只记录到显式预览完成；数据库显示保存脚本触发的另一个自动预览尚在收尾。本轮随后调整测试为等待自动预览。丢失的终端错误详情未作为已证实故障原因；这次失败不计为通过，也未调用模型。两个历史算例的全部已登记原始进程身份均已独立核验退出。
