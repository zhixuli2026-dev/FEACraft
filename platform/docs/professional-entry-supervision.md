# 专业工具入口监督：N4-03.h 首批

后续已有 [专业进程故障第二批](professional-process-faults.md)：九个入口登记拒绝及 HTTP 作业关联、真实 UDF 编译器、ParaView/CadQuery 脚本后代的取消/超时/owner 猝死已补充。下面的首批证据保留其原有范围；当前剩余以文末入口表及第二批清单为准。

2026-11-03。本批修复 CalculiX/SU2 的参数传递，核验八套真实专业工具回归中的执行回执，并补充真实 SU2 的取消、超时和 API owner 猝死验证。N4-03 仍在实施中；本批不关闭完整专业工具故障矩阵或任何 FF 整项。

## 修复的实际问题

两个求解入口原先把可执行文件和输入路径拼成字符串，经过 Windows `cmd` 启动。即使路径加了双引号，文件名中的 `%VIBEFLOW_CASE_ROOT%` 仍会被展开。隔离实例使用合法文件名 `model %VIBEFLOW_CASE_ROOT% & literal.inp` 复现 CalculiX 打开错误路径并以 201 退出。

现在两个入口将可执行文件和输入路径作为独立 `argv` 交给既有 `start_job → OwnedProcess`。展示用 command 保留；执行参数进入原有执行 spec，继续经过登记、放行和停止核验。没有修改求解器配置语义或添加 shell 转义规则。

`professional_launch_smoke.py` 对 CalculiX、SU2 分别运行普通文件名和包含空格、百分号、`&` 的文件名，共四次真实求解。检查原始输入 SHA 不变、FRD 位移数据或 VTU 产物存在、attempt/case/job 绑定、同次启动 nonce、Windows Job 成员退出完整回执，以及只读 `/jobs/{id}/process` 返回 exited。

## 本批实际证据

| 验证 | 结果和范围 |
|---|---|
| 参数与真实求解 | 4/4；两个实际求解器，修复前失败记录与修复后记录均保留 |
| SU2 停止故障 | 3/3；用户取消、测试注入 8 秒期限、真实 API owner 猝死 |
| 专业入口兼容 | 8/8：UDF 工作流、PPT、PDF 页码、CalculiX、SU2、ParaView、FloTHERM library 和 API |
| 持久回执复核 | 13 个成功 Job：WSL 5、Windows 8；另有 3 个所属原生子执行。全部父子停止回执通过严格绑定验证，全部宿主出生身份已退出 |
| 数据与部署 | 一次性 API、数据库和存储；未迁移开发库，未重启端口 8000，未运行远端推理 |

SU2 故障测试通过只读 Windows Job 查询找到真实 `SU2_CFD.exe`，在同一查询句柄上读取进程出生身份并核对成员归属。观察句柄在停止动作前关闭，避免额外 Job 句柄改变 owner 猝死时的清理行为。停止后逐个核对捕获的出生身份均 exited，范围之外的测试哨兵继续存活。取消/超时还须有完整的持久停止回执；owner 猝死只证明实际进程退出，不伪造尚未保存的数据库终态。

首次停止测试错误地把取消后的 `ended_at` 当作清理结束，读取到空停止回执；修正为等待 `attempt.process_stop` 后通过。产品原有的“取消请求已接收”和“原生进程已退出”仍是两个事实。

13 个兼容 Job 的回执是在隔离 API 停止后以只读 SQLite 打开，再调用生产绑定验证器复核。这里的 WSL 退出事实来自此次执行时保存的回执；没有额外声称做了停止后的 Linux 进程重新探测。该补充不替代 PostgreSQL、跨 API 或重启恢复验收。

规范化证据：`tmp/mainline-N4-professional-entry-evidence/`，原始路径及测试修正见其中 `evidence-origin.json`。源码基线为 `platform/backups/mainline-N4-professional-entry-20261103.zip`，SHA 和逐文件匹配另存同名摘要及证据目录。未在恢复目录另行重跑。

```powershell
platform/server/.venv/Scripts/python.exe -X utf8 -B platform/server/professional_launch_smoke.py
platform/server/.venv/Scripts/python.exe -X utf8 -B platform/server/professional_stop_smoke.py
platform/server/.venv/Scripts/python.exe -X utf8 -B platform/tools/run_regression.py --profile writes --only calculix_smoke,su2_smoke,paraview_smoke,ppt_smoke,pdf_number_smoke,udf_workflow_smoke,flotherm_library_smoke,flotherm_api_smoke
```

## 入口核对与下一工作单元

| 入口 | 当前结论 | 剩余验收 |
|---|---|---|
| CalculiX / SU2 | 固定 argv，已有通用 Job 归属；实际成功、各三种运行停止故障和两入口错误登记已验证 | 专业取消竞态、重复关闭及持久读取 |
| UDF / FloTHERM wmake | 两入口真实编译器取消/超时/owner 猝死及外部哨兵通过；错误登记通过；FloTHERM 复制分支另有 5 项，包含实际编译目录、重复取消及新 API 历史读取 | 其余竞争/跨实例通知矩阵、复制中间文件回收及正式恢复分别继续 N4-04/N5 |
| ParaView / PPT / PDF / FloTHERM 转换 | 正常结果及错误登记通过；ParaView 实际库/脚本后代三类停止、其余三个 worker 实际处理检查点三类中断通过 | 检查点不代表完整原子发布；脚本创建的 WSL 或外部服务旁路核查，不能把 Windows Job 归属推广到任意远程后代 |
| 几何与编译器库 | 注册 handler/所属 worker；实际 CadQuery 与测试脚本 detached 后代取消/超时/owner 猝死通过，停止不发布 scene | 不能声称库本身创建测试后代；跨运行时与其他可达派生、竞争和持久读取仍需核验 |
| CLI discovery | 正式 4 处调用仅文件发现；默认原生 runner 已移除，明确 callback 与无效 callback 边界已验证 | 未来 WSL 自动发现须提供所属执行器 callback，见 required-native-channels.md |
| WSL CLI 私有目录初始化 | 后续已移除直接 subprocess：固定初始化在已登记的 Linux bridge 内执行，再复验归属后启动 CLI；专项9、双库各16、实际CLI10通过 | 所属初始化见 owned-cli-state-preparation.md；残留私有目录/未知终态仍归 N5，不自动接管或删除 |
| 旧 report_worker 与条件 fallback | 已核对正式调用图；旧 worker 无正式调用者并明确拒绝运行；Pandoc/LaTeX 缺少通道时不再直接启动 | 所属准备/worker/复验走同一通道，见 required-native-channels.md；任意用户代码与恢复仍分别验收 |
| 固定身份/ancestry 读探测 | 已复现并修复 API 退出后 Linux reader 残留；现在由独立 Windows 范围、Linux 原生期限和所属管道约束，专项 11 项通过 | 见 owned-control-readers.md；统一活动与全程预算继续 N4-05/07，不是业务执行或任意命令入口 |

优先补完这些 N4-03.h 入口缺口，再推进 N4-04 持久取消、N4-05 预算、N4-06 公平调度、N4-07 统一活动/停机及 N5 恢复。完整模型配置、多模态/MCP 和账户额度仍在全功能台账中；本批不扩充或缩减 53/295 的既定范围。
