# MPI 原输入重试与当前输入重跑

2026-11-08 21:04（UTC+8），N5-05 部分验收。主线34/53、剩19；全部53项主线和295项全功能目标继续。

使用未经缩减的400单元 cavity、deltaT=0.005、100步到0.5，先实际串行 icoFoam 生成比较基准，再通过正式MPI入口执行 decomposePar、mpirun 和 reconstructPar。两种存储各验证一次实际回执文件冲突后的原封存两分区重试，再将当前顶盖速度由1改为0.5、显式修改 decomposeParDict 为4分区，新根重跑。共6个被测MPI作业，另有真实几何、网格、串行求解前置流程。

| 实际量 | FS/PostgreSQL | S3/PostgreSQL |
|---|---:|---:|
| 原两分区速度场与串行的归一化最大误差 | 1.0000000000287557e-06 | 1.0000000000287557e-06 |
| 修改当前边界后最大速度绝对差 | 0.42639699999999997 | 0.42639699999999997 |

串并行比较沿用既有1e-5限值。重试验证相同封存文件、同根attempt 2、原失败与已停止证明、同key重放；重跑验证新根attempt 1、当前初始场和分解字典哈希、实际4个rank、原生三步骤退出0、末时刻0.5和旧不可变场文件字节保留。原生回执记录非root执行及私有Linux暂存目录已清理，不将它解释成全部部署的性能或残留进程验收。

故障夹具仅在自己的私有attempt创建真实receipt冲突文件，仍调用生产放行函数。三个实际MPI步骤完成后，worker因独占写回执失败；没有替换求解程序、物理场、模型回复或推理结果。

入口为 `platform/server/live_llm_mpi_repeat_acceptance.py --source-profile-id 26 --postgres`，S3加 `--s3`。已审查其执行路径及导入辅助代码；未运行历史回归集合。模型使用本地授权的 api.deepseek.com / deepseek-flash，共4次真实HTTP调用、17128 reported tokens、模拟0。模型仅调用实际read_text_file回读报告哈希；MPI选择、求解结果、基础设施和协议断言另行验证，不宣称模型自主选择了MPI操作。

原始证据：`tmp/live-cli-startup-ht33lsob/mpi-repeat.json`（FS）和 `tmp/live-cli-startup-joqrwtu8/mpi-repeat.json`（S3），对应同目录 `live-result.json`。含两个失败轮次的四临时PG库已删除，API和三个独立S3均退出，开发数据库未写入。汇总见 `tmp/mainline-N5-mpi-repeat-evidence/validation.json`。

## S3重跑提交超时与修复

两个初轮失败现场 `wrfe0liw`、`580t4t7j` 均保留，模型调用0。第二轮原生两分区重试已发布，随后 `POST /api/v1/jobs/5:repeat` 在30.031秒超时；输入封存耗时25.297秒，重跑perform耗时29.203秒，生成的新尝试尚未进入原生MPI执行。

S3 Snapshot.materialize 原先逐文件调用 Cache.fetch，每次都重新做节点容量准入和扫描。改为既有 Cache.fetch_many，一次准入完整已知视图及日志容量；逐文件长度、哈希记录、ETag条件读取和物化前后来源版本检查保持。没有放宽30秒客户端等待或原生预算，也没有缩减算例。

同算例最终重跑请求14.578秒，封存9.031秒，其中物化2.984秒，通过实际四分区求解、发布和数值断言。FS主要证据先于这一S3专用分支优化；没有无意义地重跑FS。这里是指定故障与一次修后场景，不宣称全部S3性能或多负载SLA已验收，N7-04继续。

26类处理器中21类已有部分命名证据，5类尚缺：postprocess.case、postprocess.run、report.build、report.compose、report.compose-generated。N5-05原要求中的旧请求类型/版本、来源与工具链变化及既定崩溃交错仍需核对，不以这一场景关闭整项；MPI/批量/UDF工作流桥接仍归N3-07，完整故障矩阵仍按N7-01交付。
