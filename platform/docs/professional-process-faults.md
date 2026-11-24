# 专业工具登记失败与实际进程清理

2026-11-03，N4-03.h 第二批。在[首批入口监督](professional-entry-supervision.md)和[所属 CLI 初始化](owned-cli-state-preparation.md)之后，补充真实 UDF 编译器、ParaView/CadQuery 脚本后代的故障证据，并修复专业入口启动冲突缺少作业标识的问题。N4-03 继续实施，53 项主线和 295 项全功能范围不变。

## 启动冲突可以定位已提交的 Job

真实 UDF 登记故障复现：服务器已经提交 Job，启动门拒绝错误的进程 nonce 并清理了进程，但 HTTP 409 只有 `code` 和 `message`。客户端无法从该响应定位失败记录。

`start_job` 现在对自身抛出的 `ExecutionConflict` 附加该次 `job_id`，保留原异常实例及子类型；全局异常处理器只在存在该字段时把它放入 `detail`。例如：

```json
{"detail":{"code":"evidence_invalid","message":"Process identity is invalid for this attempt","job_id":17}}
```

状态码仍为 409。客户端可使用此 ID 查询有权限的历史详情，不应因为启动未成功就自动重新提交。没有 Job 的冲突继续省略 ID。此修改没有把其他异常统一为 409，也没有改变单独捕获并重写响应的路由；它不代表所有 HTTP 错误都带 Job ID。

`professional_registration_smoke.py` 在真实启动门登记前注入错误 nonce，依次覆盖 CalculiX、SU2、ParaView、PPT、PDF、FloTHERM 转换、FloTHERM library、UDF build 和 integrate。九个入口均返回自己的失败 Job，未放行专业载荷，宿主和适用的 Linux 出生身份均退出。另验无 Job 冲突不编造 ID、外部用户不能通过 ID 读取这些作业，共 11 项。UDF integrate 的提交前配置准备仍可发生；本测试并不声称整个请求没有任何文件写入。

## 实际执行和停止证据

| 套件 | 结果 | 证明范围 |
|---|---|---|
| `udf_scope_smoke.py` | 4/4 | 错误登记、取消、8 秒测试期限、实际 API owner 猝死；真实 WSL `wmake` / `g++` / `cc1plus`，重新探测捕获的出生身份均退出，源文件 SHA 不变，外部 WSL 哨兵存活 |
| `professional_library_scope_smoke.py` | 6/6 | ParaView 和 CadQuery 各三种停止场景；真实球面/立方体库调用后，显式测试脚本创建 detached 子进程，验证父子均属于 Windows Job，停止后所有捕获成员退出，外部宿主哨兵存活 |
| 专业入口错误登记 | 11/11 | 上述九个 HTTP 入口以及无 Job / 跨用户读取边界；未执行专业载荷 |
| 执行器兼容 | 6/6 套件 | 几何发布、领取、API 幂等、进程登记、handler、状态转换 |
| 通用进程登记双库 | SQLite 20/20、PostgreSQL 20/20 | 真正的宿主/WSL 启动门、登记失败与取消、两代 API 只读读取；临时 PostgreSQL 数据库已移除 |

UDF 测试给隔离样例增加有界的简单函数，捕获实际正在工作的 `cc1plus`；没有用休眠包装器代替编译器。错误登记场景不进入编译，其余三场景观察实际编译后代。

ParaView/CadQuery 测试中的 detached 子进程由测试用户脚本明确创建，不声称是 VTK/CadQuery 库自身启动的进程。停止前关闭只读 Windows Job 观察句柄，以免观察行为延长 Job 寿命。几何测试检查原脚本未变且没有发布活动 scene/exports。它没有验证任意脚本启动的 WSL、外部服务或远程任务都受 Windows Job 控制，`complete_process_tree=False` 的几何策略保持不变。

取消和超时等待实际停止回执，并校验 `verified`、停止原因和作用域清空。API owner 猝死只以实际 owner 和后代出生身份退出为证，不伪造未保存的终态；持久记录对账仍属 N5。

## 可复核记录和测试修正

规范证据在 `tmp/mainline-N4-professional-scopes-evidence/`，`evidence-origin.json` 列出每个原始目录、日志及修复前响应。源码基线为 `platform/backups/mainline-N4-professional-scopes-20261103.zip`；摘要见同名 `.sha256`，逐文件匹配见证据目录。没有在恢复目录重新运行。

初次几何测试保存脚本后又显式启动一次预览，捕获文件来自保存接口自动启动的第一项作业，成员查询却针对第二项作业，因而错误失败。测试改为使用保存响应的 `auto_preview_job` 后，两种工具的全部六项通过。该失败不是产品进程逃逸证据。两次失败日志保留，没有把它们算作通过。

```powershell
platform/server/.venv/Scripts/python.exe -X utf8 -B platform/server/udf_scope_smoke.py
platform/server/.venv/Scripts/python.exe -X utf8 -B platform/server/professional_registration_smoke.py
platform/server/.venv/Scripts/python.exe -X utf8 -B platform/server/professional_library_scope_smoke.py
platform/server/.venv/Scripts/python.exe -X utf8 -B platform/server/process_registration_smoke.py --postgres
```

三个新套件由 `all` 自动发现，需要相应本地专业运行时；没有加入默认离线集合。全部验证使用一次性 API、数据库和存储，开发数据库、端口 8000 和远端推理未动。本批无 UI、DDL 或迁移修改；未重跑 UI、CAE 或全量回归。

## 继续推进

1. 下述后续批次已补 CalculiX/FloTHERM 编译，以及 PPT/PDF/FloTHERM 转换的实际处理检查点中断；继续核对这些入口尚缺的取消竞争、重复关闭和持久读取场景。
2. 旧 worker、条件 fallback 和只读身份探测已由后续批次核对并修复，见 [所属编译通道](required-native-channels.md) 与 [所属只读身份探测](owned-control-readers.md)。跨运行时/外部服务仍须另立可执行边界，不能由 Windows 子进程试验推断覆盖。
3. 按主线推进 N4-04 持久取消和发布竞争、N4-05 预算、N4-06 公平调度、N4-07 活动与停机，再进入 N5 恢复。

商业部署继续采用 API / Sign in with ChatGPT；网站身份与模型凭据分开。正式网站身份供应方配置仍待实际联调，不影响本批执行器工作。其他模型能力、MCP、认证刷新及完整功能清单继续保留在既定台账中。

## 后续：其余专业运行中断

2026-11-03，在第二批源码冻结后，增加如下验证。此次仅修改测试及文档，产品实现沿用第二批。

| 入口 | 结果 | 中断位置与范围 |
|---|---|---|
| CalculiX | 3/3 | 同一八节点物理模型的有界 20,000 步静力 deck，输入小于 1 MiB；捕获真实 `ccx` 成员，并读取 STA 中已完成的增量后取消/超时/终止所属 API |
| FloTHERM library | 4/4 | 正式 build-library 入口、实际 WSL wmake/cc1plus：错误登记、取消、超时和 API 猝死；源目录等于目标目录的分支，不声称覆盖复制源目录分支 |
| PPT / PDF / FloTHERM 转换 | 9/9 | 各三种停止场景；在真实 worker 完成表格创建、页码内容生成、转换 case 生成后，测试 wrapper 插入最长 60 秒暂停，再取消/触发 8 秒作业期限/退出所属 API |
| 共享编译夹具兼容 | UDF 4/4 | 提取 `verify(backend)` 后重跑原 UDF 四场景；两入口各自独立 API、数据库和存储 |

`calculix_scope_smoke.py` 不替换求解程序或加入休眠。STA 原始进度、成员出生身份、输入 SHA、停止回执一起保留。`flotherm_compile_scope_smoke.py` 复用 UDF 编译夹具的真实编译工作量和独立 Linux 进程探测，经自己的 HTTP 入口启动。

`professional_worker_scope_smoke.py` 使用独立夹具 wrapper 通过 `runpy` 执行未修改的产品 worker，并核对文件 SHA；profile 回调只在目标实际函数返回后记录本进程出生身份和处理结果，然后进入明确的故障暂停。PPT 检查实际 3×2 表格，PDF 检查三页输入的真实页码内容，FloTHERM 检查生成的 blockMeshDict 和 case manifest。这些是受控检查点中断测试，不是无仪器介入的耗时测量，也不是用模拟结果替换真实函数。

全部场景要求捕获的实际成员退出、外部哨兵继续存活、输入及已有无关文件 SHA 不变；取消和超时另要求完整停止回执。API 猝死仍不制造持久完成状态。PPT/PDF 在本检查点尚未写最终输出；FloTHERM 可能保留已生成的部分输出目录，但尚无最后的 IR 输出。不能将其解释为所有阶段的原子发布或失败清理已经完成，这仍由 N3/N4-04/N5 的相应任务处理。

证据：`tmp/mainline-N4-professional-solvers-evidence/`，包含原始路径、实际工作检查点和回执。源码：`platform/backups/mainline-N4-professional-solvers-20261103.zip`，摘要及逐文件匹配同前。新套件仍只由 `all` 自动发现；本次没有重跑刚通过的双库、六套执行器兼容、UI、CAE 或全量回归，没有产品/DDL 修改，也未迁移开发库。

```powershell
platform/server/.venv/Scripts/python.exe -X utf8 -B platform/server/calculix_scope_smoke.py
platform/server/.venv/Scripts/python.exe -X utf8 -B platform/server/flotherm_compile_scope_smoke.py
platform/server/.venv/Scripts/python.exe -X utf8 -B platform/server/professional_worker_scope_smoke.py
platform/server/.venv/Scripts/python.exe -X utf8 -B platform/server/udf_scope_smoke.py
```

## 后续：FloTHERM 复制编译和历史读取

2026-11-03，补充 `source_dir != destination_dir` 分支。`flotherm_copy_scope_smoke.py` 经正式 HTTP 接口复制源码，使用真实 `wmake` / `cc1plus`，四种启动/停止场景与一组新 API 历史读取共 5 项通过；共享夹具的 UDF、FloTHERM 原同目录分支各 4 项复验通过。本批没有修改产品实现。

- 错误登记：返回关联 Job 的 409，原生启动门退出；已复制文件完整保留，目标尚无编译目录。
- 取消、8 秒测试期限、API owner 猝死：先观察真实 `cc1plus`，在复核出生身份前后读取 `/proc/<pid>/cwd`，确认编译发生在副本目录。停止后捕获的成员出生身份全部退出，副本无最终动态库，外部 WSL 哨兵继续存活。
- 复制来源的全部文件清单和 SHA 完全不变；副本中的每个源文件 SHA 与来源一致。编译产生的中间文件可以保留，不将此检查解释为失败后的目录自动回收。
- 运行中并发提交两个取消请求，至少一个被接受；清理完成后再次取消返回非接受，持久停止回执不被改写。这验证现有响应契约，N4-04 的最终取消幂等接口仍单独推进。
- 新 API 使用同一隔离数据库和存储读取四项历史，attempt、进程身份和停止回执一致。API 猝死项沿用现有启动对账标为 interrupted，不能凭此补造停止回执或宣称 N5 恢复已实现。查询进程状态、跨用户拒绝及目录清单不变一并核验，没有重新执行编译。

首轮已通过四个实际中断场景，但历史读取测试误把“没有已登记身份”的查询状态写成不存在的 `not_registered`。产品契约为 `unverified / invalid_or_legacy_identity`；修正测试后完整 5 项通过。首次失败日志及最终日志分别保留，没有产品状态变更。

证据：`tmp/mainline-N4-flotherm-copy-evidence/`；源码：`platform/backups/mainline-N4-flotherm-copy-20261103.zip`，摘要及逐文件匹配另存。本批 SQLite 读回不替代其他专业入口或 PostgreSQL 的完整矩阵；未迁移开发库、重启端口 8000、运行远端推理或重跑全量/UI/CAE。

```powershell
platform/server/.venv/Scripts/python.exe -X utf8 -B platform/server/flotherm_copy_scope_smoke.py
```
