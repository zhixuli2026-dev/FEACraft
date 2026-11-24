# 正式UDF构建与集成工作流

2026-11-09，N3-07子阶段。functionObject基准在FS/PG、S3/PG共享缓存及S3/PG独立空缓存三轮通过；仍不是N3-07整项完成。主线36/53完成、剩17项，295项全功能范围不变。

## 接口与来源

创建/重算/迁移参数增加可选solve_mode="udf"及必需udf_name，例如parameters={"geometry_mode":"export","solve_mode":"udf","udf_name":"myFunctionObject"}。subdomains仅适用MPI，udf_name仅适用UDF；未传新字段的旧参数保持原样。当前实际集成重用已有icoFoam functionObject管线，不宣称所有自定义求解器、边界、源项或库类型均已完成。

C明确确认后先运行udf-build步骤（udf.build.workflow），发布独立udf-build版本，含实际ELF、wmake与CDLL日志及内嵌原始原生凭据；再运行solve.openfoam.udf，固定本流程的网格版本及本次库版本，只挂载各输入一次。仍使用原UDF worker/native/artifacts、非root所属执行和真实结果验证。派生controlDict及编译缓存留在私有尝试，正式solve只发布本次场、时刻、UDF日志/证据等结果；没有伪造串行solve-evidence。别名复制继续绑定原输入摘要，损坏bundle的受管路径回退也识别新生产者。

D确认使用udf-evidence.json，求解门控核对原输入/实际结果集合，后处理固定solve、mesh和库来源。UDf名称参与重算匹配；按模式展开的生产者顺序包含udf-build，C++源改变会重建库、求解及下游，已有新鲜库/求解前缀可复用且没有新Job，B/C/D/E仍需明确确认。原v1/v2串行/MPI325组合及v3前缀75组合与上一归档逐项一致，这400项是纯计划协议检查，不当作400次模型或原生执行。

连续的构建→求解节点需要持久续派。workflow_dispatch扫描SQL中已注册、非终态流程的就绪原生步骤及已结束尝试，按算例/账户分层并以4个在途调度推进；仍由原注册图、访问校验、case锁、幂等提交与独占claim决定是否执行。它不确认人工节点、不重放失败/取消运行，也不启动旧v1命令；启动/停止已接入app。完整调度故障、公平性、资源边界及可见拒绝原因仍按N4/N6/N7原条件继续，不能由本批正向链称全套完成。

## 真实验收

| 组合 | 证据根 | 模型回读 |
|---|---|---:|
| FS/PG | tmp/live-cli-startup-m93dpmzy | 2次，8572tokens |
| S3/PG，共享缓存先行 | tmp/live-cli-startup-o2r7rlws | 2次，8572tokens |
| S3/PG，独立空缓存 | tmp/live-cli-startup-9qkk1lst | 2次，8766tokens |

每轮实际wmake生成全新ELF、实际CDLL加载，icoFoam加载固定库并输出UDF_WORKFLOW_V1，400单元真实场达到0.5；原controlDict保持，mesh/库/solve来源fresh。实际终止源API后，在新API查询同一历史与Job集合，不重跑构建/求解，D/E明确确认后才完成实际压强提取与readiness。独立缓存轮在重启前断言新缓存根不存在，原/新API缓存目录不同，随后从远端读取完整库、网格、求解和后处理来源；这不是物理多主机验收。

再修改实际C++为V2，依赖检查表明只有库与下游过期、几何网格仍fresh；明确A重算和B/C确认后实际重编译得到不同ELF，真实求解输出V2，再明确D/E完成，旧成功历史保持。最后规划缺失报告，实际复用完整几何/网格/库/求解/后处理前缀，库与求解步骤无新尝试，B/C/D仍分别确认，E之前取消且Job集合不增。本检查不声称本轮生成了报告。

另用tmp/live-cli-startup-6f6vpg5g完整复验真实串行→2rank MPI、D/E/压强提取与API终止后继续；速度相对误差仍不超过原1e-5，说明新注册节点及续派没有破坏该命名旧链。

以上本批合计8次api.deepseek.com / deepseek-flash真实HTTP200、34472已报告tokens，模拟0，全部最终usage已记录。模型调用仅为独立读取实际证据摘要，不宣称模型自主规划或编写了UDF。原生执行、真实PG/S3、协议/故障各自记录。S3先行最慢请求27.218秒，独立缓存轮的具体请求计时在udf-workflow-http.jsonl；整体性能SLA继续原任务。

## 失败、边界与后续

0ao8q6c2实际构建、载入、求解与重启/后处理已运行，但新验收错误请求不存在的postprocess/latest接口，模型0，原失败保留；改为读取实际发布的metrics.json。9vz5st_a在重启整体历史相等断言失败，模型0；代码当时在等待/排空前保存快照，原轮未保留前后差异，因此不能补造具体差异字段。之后在真实收尾后重新取完整快照并保存前后两份，独立缓存轮严格整体相等通过。FS先行使用较早的快照记录代码；其完整编译/重算/复用断言实际通过，执行源码另存entry-source.py，未改写旧证据。

本阶段构建语义来源为UDF/name（排除原生编译缓存）和Ref，原生仍封存整个算例。UDF/Ref之外的本地Make/include/link依赖闭包、缺失/冲突库别名时直接从固定发布准备、真实编译/求解运行中取消与明确恢复、所有其他UDF类型和完整部署故障矩阵尚待补齐。当前源码不能据此声称完整来源策略已完成。类型多工况/额度/归属/Redis与原S3 UDF单次409也继续；新的独立缓存成功不是原409已归因的证明。N3-07原四条remaining完整保留，N6-03亦仅增加续派子能力。

6个临时PG库已删除，3个自建S3服务已停止，只读进程复核无本批驱动/API残留；开发库仅只读获取授权profile26，未运行历史模拟LLM回归。UDF/MPI原生及所属执行器、持久投递/停止/额度等列明核心文件与上一归档字节一致。

真实入口live_llm_udf_workflow_acceptance.py --source-profile-id 26 --postgres，可加--s3，使用项目.venv的Python -X utf8 -B。完整源码platform/backups/mainline-N3-07-udf-workflow-20261109.zip，SHA/AST及证据审计见tmp/mainline-N3-07-udf-workflow-evidence/validation.json。下一步补来源策略、UDF运行中取消/恢复，再推进类型多工况和原剩余条件。
