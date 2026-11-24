# UDF本地编译输入来源专项

2026-11-09，N3-07子阶段。真实GCC、GNU make、Make预处理及链接器记录已接入封存输入来源；本地和S3命名场景通过。主线36/53、剩17项，295项全功能目标不变；完整来源策略仍继续，不关闭N3-07。

## 实现

新构建启用GCC的-MD和GNU ld的--dependency-file，记录实际C++依赖与链接器读取的对象/库/脚本输入。wmake会主动清除早期预处理阶段的MAKEFLAGS，故由私有固定入口exec真正的/usr/bin/make，仅加debug/trace观察参数，保留原argv、配方和返回码。实际Make/files、Make/options预处理命令被记录，再由真实cpp以同样参数生成依赖；GNU make读取的包含文件也入清单。没有替代编译器、make、CLI推理或模型结果。

收集器由准备配置中的SHA绑定，原生启动前验证。实际消费的case本地依赖必须来自本次原始封存清单，逐字节核验后才记录；不从编译结束时的当前可变文件反推来源。非封存case、非受管运行时的依赖拒绝。编译器明确生成的.o/.d及原UDF缓存/库/日志与源输入区分。受管运行时路径另记为观察证据，complete_runtime_closure=false，不冒充完整外部工具链闭包。

发布描述符的source增加compiler部分，绑定直接输入与本地-I/-L等搜索目录的保守文件清单，检测内容变化及新增同名文件。缺失直接输入保留None进行来源比较，而不是把已知变化一概报不可验证。集成步骤沿用固定构建发布的编译来源；旧发布缺少编译来源时保持历史可读，新的明确重算需重建库，不自动重放旧作业。Case源码和发布绑定仍经过原来源围栏、固定库/网格和结果校验。

## 真实验收

| 组合 | 证据根 | 真实DeepSeek调用 / tokens |
|---|---|---:|
| dependency_fs | tmp/live-cli-startup-q5__4qyy | 2 / 8634 |
| dependency_s3 | tmp/live-cli-startup-hv0rn5ap | 2 / 8814 |
| normal_fs | tmp/live-cli-startup-2l2p9fh3 | 2 / 8640 |
| normal_s3 | tmp/live-cli-startup-ku42p2ys | 2 / 8664 |

两轮dependency各先完成400单元实际wmake/CDLL/icoFoam，终止源API，在新API读同一历史；S3为独立空缓存。搜索范围外的无关文件改变后，库和求解仍fresh。随后只修改外部头文件、CPP包含的Make配置、GNU make包含文件、真实非root g++提供的链接对象，以及加入更高优先级的同名头文件。五轮主C++字节均不改变，几何/网格保持fresh，库/求解变stale；明确A重算/B/C/D确认后只重建库与求解，产生不同ELF，400单元真实日志输出对应的新值，旧历史和幂等键保持。每轮所记外部输入都与原始封存清单完全一致。

两轮normal完整复验原functionObject链的构建/加载/求解、D/E和实际提取、API终止后继续、改C++重建不同ELF、已有库/求解及下游前缀复用；没有把依赖专项替代原下游场景。

本批合计8次api.deepseek.com / deepseek-flash真实HTTP200、34752已报告tokens、模拟0，最终usage完整。模型仅独立读取实际证据，不宣称模型编写或自主规划了C++。6个临时PG库已删除、2个自建S3服务已停止，只读进程复核无本批残留；开发库只读获取授权profile26，未运行历史模拟回归。

先行g858whym实际已链接ELF，收集器因缺失Make预处理跟踪拒绝，原失败保留；按本机wmake源码改为真实make私有入口。7x2g40ha的新夹具CPP包含相对路径少一层，预处理失败并导致编译找不到external.H；仅修正夹具相对路径，原失败保留，两轮模型0。另一次临时链接器命令的引号错误产生Python语法失败，修正后实际GNU ld依赖文件格式已核验；不是模型或产品验收。

## 尚未完成的原范围

当前本地搜索目录清单是保守来源策略。宽根目录（例如-I../../）在工程输出/确认变化下的来源策略、动态Make配方读取、响应文件及其他参数形式的完整闭包仍待完善；不能把五种命名改源成功称全部本地来源已完成。受管工具链/系统头文件的完整可变环境矩阵继续原N7目标。

真实非root strace/GCC基础设施探针已证明：同一C++下，可观察optional.H缺失和存在的实际文件查询，新增文件改变实际对象SHA。证据tmp/UDF-compiler-open-20261109.json。该探针模型0、模拟0；生产工作流尚未接入这些系统调用记录，不把它当完整来源交付。

N3-07继续完整来源、多工况桥接/配额/归属/Redis重启和原S3 UDF单次409归因，原四条remaining保留。所有承诺UDF类型及完整G5由FF-A10/FF-C10继续，不缩减295项，也不追加为N3-07原条件之外的新关卡。

源码platform/backups/mainline-N3-07-udf-compiler-inputs-20261109.zip；SHA及全部源文件/Python AST核验见tmp/mainline-N3-07-udf-compiler-inputs-evidence/validation.json。真实入口live_llm_udf_dependency_acceptance.py --source-profile-id 26 --postgres，可加--s3。
