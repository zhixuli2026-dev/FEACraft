# UDF实际文件查询与条件输入

2026-11-09，N3-07子阶段。实际编译进程的文件查询已纳入case来源。主线36/53、剩17项；N3-07仍进行中，295项全功能范围不变。

## 实现和边界

真实wmake及其子进程由strace观察文件/进程系统调用和getdents64，保留原编译器、参数、返回码。按实际fork/共享cwd、工作目录和目录fd还原case相对路径，结合已有GCC/CPP/Make/GNU ld依赖记录。来源来自准备时封存的原始输入清单；实际读取的已有文件逐字节核验，文件缺失、文件/目录类型和实际枚举目录的直接成员分别记录。宽-I../../只追踪实际查询，工程输出或B/C/D确认不会令自身来源过期。新增同名可选头文件等查询结果变化会使库和求解过期。

V2保留成功/失败chdir所观察的目录条件，也保留生成文件之前已观察到的缺失条件；后来用户在同一路径提供文件时必须重编译。生成物与可信编译缓存排除规则仍保留。旧V1发布历史可读，明确重算时重建缺少当前来源契约的库，不自动重放旧作业。

观察限制为1024个追踪文件、64MiB和500000条事件；原生执行另有周期监控超限停止和退出后解析检查。周期监控允许短暂超量，不能称严格无超量磁盘上限。原始strace文件在私有阶段清理，发布凭据保留各文件摘要、大小、事件数及查询投影；不能声称已归档原始追踪全文。受管运行时路径只记观察，完整环境/工具链可变矩阵仍按原N7范围验收；本专项不承诺任意环境、网络、时间条件的可重现构建，也不代替FF-A10/FF-C10的所有UDF类型。

S3选择读取一次已有公共逻辑目录，避免数百个缺失候选分别触发对象历史遍历。保留原选择上限、路径校验、前后版本围栏和实际条件读取；没有HEAD的旧case沿用原只读路径，不创建或迁移目录。真实600缺失候选仅1次GetObject，结果与旧列表一致，私有历史排除、旧版本读取拒绝、同字节ABA和删除条件均验证；该专项模型0。

## 实际验收

| 组合 | 证据根 | 真实DeepSeek调用 / tokens |
|---|---|---:|
| normal_fs | tmp/live-cli-startup-m_mh3yi3 | 2 / 8692 |
| normal_s3_before_catalog | tmp/live-cli-startup-8xtogj06 | 2 / 8576 |
| query_v1_fs | tmp/live-cli-startup-ywjq9f9p | 2 / 8574 |
| query_v1_s3 | tmp/live-cli-startup-dy1kr4xp | 2 / 8572 |
| cancel_fs | tmp/live-cli-startup-v32mz666 | 2 / 8568 |
| normal_s3_catalog | tmp/live-cli-startup-y9i8g4c4 | 2 / 8566 |
| cancel_s3 | tmp/live-cli-startup-rzynzwb2 | 2 / 8572 |
| query_v2_fs | tmp/live-cli-startup-h7rvh_gi | 2 / 8568 |
| query_v2_s3 | tmp/live-cli-startup-cis46rdv | 2 / 8656 |

V2每个后端先完成400单元wmake/CDLL/icoFoam，然后实际终止源API；新API读回同一历史，S3使用独立空缓存。无关诊断文件不改来源。随后依次只改Make动态读取文件、编译器响应文件、添加原先缺失的可选头文件、增加枚举目录成员、修改宽根头文件、创建条件chdir目录、提供原先由构建生成的头文件，共7轮。主C++始终不变；几何/网格fresh，库/求解stale；明确重算只重建库/求解，8份ELF各异，真实日志输出对应7个宏值，旧历史和同键幂等保持。

V1两后端各覆盖前5种变体。V1还复验原functionObject完整下游链，以及实际cc1plus、CDLL和已加载UDF的icoFoam三阶段跨API取消/排空/源API退出/明确重算；三阶段均核验旧求解发布和场保留，编译/加载取消另核验旧库保留，求解取消前已发布新库，因此不要求恢复更早的库。原生监督实现与V2相同，V2只改变查询解释及新增条件算例；不把V1两项已发现缺陷称已通过。早期normal_s3_before_catalog未使用新S3读取优化，normal_s3_catalog及V1/V2 S3查询和取消覆盖当前优化。

本批合计18次api.deepseek.com / deepseek-flash HTTP200、77344已报告tokens、模拟0。模型独立回读真实证据，不宣称模型自主编写或规划全部工程工作流。10个临时PG已删除、7个自建S3服务已停止，所属API/驱动只读进程复核无残留；开发库只读获取profile26，未运行历史模拟集合。

失败均保留：ttxjaxqf夹具把GNU wildcard放进经CPP预处理的Make/options，/*被识别为注释；改为实际GNU make包含的compiler.mk。6g0vq4ak真实S3比较发现新目录ETag带引号而旧选择不带，修正格式后x8az7v5h通过。另有真实非root进程反例证明V1遗漏chdir条件和生成前缺失条件，均造成实际结果变化却未判过期；V2实际进程复验和完整双后端8轮工程链已通过。失败证据不得删除或算入通过数。

中间源码包945c8076...固定了V1收集器、原生执行版本和当时查询入口；两项失败反例另存tmp/UDF-query-conditions-counterexample-20261109.json，并汇入最终validation。中间包已包含稍后加入的S3选择优化，不是早期两轮normal的完整执行树证明。最终V2源码及全部AST/哈希核验见tmp/mainline-N3-07-udf-file-queries-evidence/validation.json。原生监督、worker及公共生命周期保护模块保留原实现。

## 下一步

转入正式类型多工况批次：固定成员/输入/模式、持久父子工作流关系、原子校验及幂等、用户与批次额度、归属、跨API取消和真实Redis重启。原S3 UDF单次409仍须按原记录归因，后续成功不能替代原因证据。N3-07原四条remaining完整保留；所有承诺UDF类型和全功能最终验收不缩减。
