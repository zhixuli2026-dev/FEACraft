# Flotherm 与 UDF 重试、重跑及时间线修复

2026-11-08，N5-05 部分验收。主线 34/53、剩 19 项；53 项主线及 295 项全功能目标保持。

## 实际结果

| 处理器 | FS/S3 实际验收 |
|---|---|
| flotherm.convert | 实际 IR/settings 转换器完成后遭遇回执冲突；原封存重试发布 5 W 结果，当前 settings 以 7,500 mW 输入在新目录重跑，转换覆盖报告为 7.5 W |
| flotherm.build-library | 实际非 root wmake 编译及 CDLL 加载后遭遇回执冲突；原输入重试及当前 C++ 新目录重跑均发布 ELF，独立非 root WSL 观察器实际调用已核验不可变库的导出函数，分别返回 1 和 2 |
| udf.build | 实际非 root wmake/CDLL 后遭遇回执冲突；原输入重试，当前 C++ 重跑；真实 ELF 中包含各自源码的标识，旧库字节保留 |
| udf.integrate | 基于实际几何、网格、串行求解并经 B/C 确认的 400 单元 cavity；真实库被 icoFoam 加载后遭遇回执冲突，原输入重试发布至 0.5；当前边界速度由 1 改为 0.5，新根重跑至 0.5，实际速度场最大绝对差均为 0.42639699999999997 |

四类共 24 个被测失败/重试/重跑作业，另有 UDF 接入前真实串行流程等前置工作。每类验证原封存文件相等、同根 attempt 2、新根当前输入、正式发布、同 key 唯一执行和旧不可变版本保留。Flotherm 是本项目的真实转换器及辅助库流程，没有宣称执行商业 Flotherm 求解器。库函数观察器是独立、只读的实际 WSL 观察进程，其运行不作为产品作业的原生归属证明。

入口为 `live_llm_flotherm_repeat_acceptance.py`、`live_llm_udf_repeat_acceptance.py`，带授权 profile 26 和 `--postgres`，S3 另加 `--s3`。所有故障在独立测试应用自己的私有目录创建真实冲突文件，继续调用生产放行函数，没有替换进程或模型输出。

## 修复时间线误判

最初 Flotherm 与 UDF 原生失败后，重试均误报 input_stale。保存现场逐文件比较证明唯一变化是 `0-caseDict/case-events.jsonl`：旧字节前缀完整，平台追加了失败事件，业务输入未变。

将既有严格前缀验证放到共用 `case_file_delta`，供整算例来源的处理器使用。只有原前缀 SHA、大小、执行位保持，新增行完整、schema 正确且整数 case ID 属于同一算例时，来源比较才还原封存前缀；重试仍复制原字节。业务文件、原历史改写、无效行、其他 case 或布尔值 ID 均不忽略。原 CaseCommand 导入名保留，兼容原调用。

原生 delta 计算同时拒绝 worker 改写或删除平台时间线；即使重写相同字节导致 mtime 变化，也不会把它列为原生产物。历史产物 delta 也不能替换或补造这份来源记录。S3 仍按实际文件重新计算目录集合。

回归 `live_llm_typed_timeline_acceptance.py --postgres` 的七类检查通过：四类前缀/追加/归属负例、合法失败事件追加、两个实际 Python 覆盖/删除场景。新增 `live_llm_delta_timeline_acceptance.py --postgres` 用实际 pvpython 生成 50 点/96 单元数据，再真实覆盖或删除私有时间线；worker 拒绝发布，活动历史前缀保持，原生输出未进入活动算例。第三个真实场景重写相同字节，允许正常产物发布，并验证发布清单不含平台时间线。

## 修复 UDF 当前输入重跑

时间线修复后，实际 UDF 接入重试已经成功发布，但当前 controlDict 中已存在刚发布的函数注册，重跑又被重复注册判断挡住。增加处理器专用 `prepare_rerun`，只有显式 rerun/重新准备才可复用唯一、规范内容完全匹配的显式条目，不重写其他控制文本，也不重复注册。

初次普通提交仍拒绝重复注册。其他名称、其他库、额外配置或第二个同类型条目仍拒绝，并返回 function_exists。八项独立协议检查覆盖严格初次提交、相同/注释条目复用、冲突及历史时间线不能被产物掩盖。两种存储下的实际重跑均只含一个函数注册，并输出真实 UDF 标识与新的速度场。

Flotherm 已有目标改为明确的 output_exists，保持已有内容，要求选择新目录；不再将普通目标冲突误报为恢复证明缺失。

## 证据、失败与范围

| 主要证据目录（均在 tmp/ 下） | 报告 |
|---|---|
| live-cli-startup-3mt0qf5w | flotherm-repeat.json，FS/PG |
| live-cli-startup-0at19wt4 | flotherm-repeat.json，S3/PG |
| live-cli-startup-jx_ff6gm | udf-repeat.json，FS/PG |
| live-cli-startup-lgxhl_1e | udf-repeat.json，S3/PG |
| live-cli-startup-2xrtvu3p | delta-timeline.json，实际 pvpython 三场景 |
| live-cli-startup-7twaypd1 | typed-timeline.json，共用前缀校验回归 |

六个主要成功运行共 12 次真实 `api.deepseek.com / deepseek-flash` Agent 工具回读、51,614 reported tokens；先行 Flotherm FS 版本 `h8kksn_w` 与两场景时间线版本 `zb5aqhrs` 另 4 次、17,146 tokens，合计 16 次、68,760 tokens，模拟调用 0。模型回读只认证场景报告哈希，实际原生执行、函数返回、场数据、纯协议和故障断言分别验收，不宣称模型选择了这些操作。

三个整轮失败均保留，模型调用 0：`60d640fa`、`70svpcx8` 因平台时间线追加误判；`85fi6nyh` 在真实 UDF 接入重试发布后，因重复函数注册导致重跑被拒。先行通过的 `h8kksn_w` 仅绑定时间线修复版本，目标冲突当时仍返回 recovery_required；最终 FS/S3 已验证 output_exists。

最后补充的同字节时间线重写排除，由实际 pvpython 三场景检查覆盖；四类业务流程及原时间线回归未为这一分支再次全量执行。

十一个临时 PostgreSQL 库（含失败与先行版本）已删除，API/独立 S3 服务均已退出，开发数据库未修改。源码及分项验证清单为 `tmp/mainline-N5-flotherm-udf-repeat-evidence/validation.json`。

归档时发现原允许列表漏收 C/C++ 模板、Make/files/options 和 SU2 网格。现已补齐这类源码资产，并对本项目 UDF 四个构建输入及 NACA 网格逐字节核验入包；旧 ZIP 保留原样。源码归档仍排除开发数据和私有配置，外部已安装运行时不作为源码复制。

注册处理器 26 类中，20 类已有部分命名操作证据，6 类尚缺：MPI、两类后处理、三类报告。其他适用模式、来源/工具链变更、旧请求类型/版本与既定崩溃交错继续，N5-05 未计整项完成。全功能 295 项最终交付继续。
