# 从固定发布准备原生输入

2026-11-09，N3-07子阶段。主线36/53完成、剩17项，295项全功能范围不变。本阶段不关闭N3-07。

原生UDF准备此前读取可变兼容目录中的库；库别名缺失时，即使正式工作流已经固定了有效库发布，也无法完成准备。MPI原生源清单同样可能遗漏缺失的已发布网格。现在先逐字节核验所固定发布的文件，再将其元数据叠加到原生输入清单；封存仍从不可变发布读取真实字节。当前目录中的其他输入及原验证、大小/数量上限不变，目录/文件冲突拒绝。UDF库存在性检查也使用已解析的发布根，MPI生成配置与叠加后的实际清单一致。产品准备阶段不修补可变别名。

## 实际验收

| 入口 | 存储/数据库 | 证据 | 真实DeepSeek调用/tokens |
|---|---|---|---:|
| UDF | FS/PG | tmp/live-cli-startup-5rmqylav/udf-pinned-inputs.json | 2 / 8574 |
| UDF | S3/PG，恢复API独立空缓存 | tmp/live-cli-startup-f2r9_xky/udf-pinned-inputs.json | 2 / 8574 |
| MPI | FS/PG | tmp/live-cli-startup-y2gje04t/mpi-pinned-inputs.json | 2 / 8690 |
| MPI | S3/PG | tmp/live-cli-startup-0yx3w_nn/mpi-pinned-inputs.json | 2 / 8562 |

UDF在第一次实际编译发布后删除兼容ELF，第二次改C++真实重编译后写入错误别名字节。两次实际icoFoam加载的库SHA均等于对应固定发布，缺失/错误别名始终未被产品修复。每轮继续完整原基准：400单元到0.5、D/E明确确认、实际压强提取、已完成后源API实际终止及新API继续、改源新ELF、旧历史保持及新鲜前缀复用。S3恢复API使用独立空缓存。FS先行使用缺失文件捕获的较早入口，之后为S3改为exists检查；每轮实际执行入口已分别另存，旧证据未改写。

MPI在C确认后真实删除points并损坏faces，仍使用固定网格实际完成2rank分解、求解及重构，与串行场相对误差约1e-6，小于原1e-5阈值。测试夹具在原生结果验证后，仅恢复其自己创建的两处故障，再继续原D确认和后处理门控；这是夹具的明确恢复行为，不能称产品自动修复别名，也不能称任意错误工作目录可通过D确认。原API终止后继续、明确D/E及提取通过。MPI本轮不宣称S3恢复缓存相互独立。

四轮共8次api.deepseek.com / deepseek-flash实际HTTP200，34400已报告tokens，模拟0，最终usage完整。模型仅独立回读实际证据，不宣称模型自主规划或编写原生算例。故障夹具只改变隔离算例的实际存储字节，不替代LLM或原生执行结果。未执行历史模拟LLM回归。4个临时PG库已删除，2个自建S3服务已停止，进程复核无本批API/驱动残留；开发库只读获取已授权配置。

## 仍需完成

本阶段没有完成UDF/Ref之外的用户Make/include/link依赖来源策略，没有验证运行中的真实编译/载入/求解取消及明确恢复，也没有实现类型多工况完整桥接。原S3 UDF单次409仍未归因。N3-07原四条remaining保留，任意跨主机部署、资源/性能和最终故障矩阵继续原任务；本轮最慢S3请求22.391秒不是整体性能SLA。

源码包：platform/backups/mainline-N3-07-pinned-native-inputs-20261109.zip；SHA、全部源文件和Python AST核验在tmp/mainline-N3-07-pinned-native-inputs-evidence/validation.json。真实入口为platform/server/live_llm_udf_pinned_input_acceptance.py和live_llm_mpi_pinned_input_acceptance.py，参数--source-profile-id 26 --postgres，可加--s3。
