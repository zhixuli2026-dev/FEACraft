# 工程处理器重试、重跑与旧请求重新准备

2026-11-08，N5-05 部分验收。主线仍为 34/53，剩余 19 项；全部 53 项主线及 295 项功能范围保持。

## 三类实际工程处理器

`live_llm_engineering_repeat_acceptance.py --source-profile-id 26 --postgres`，以及加 `--s3` 的入口，均通过。FS 证据目录是 `tmp/live-cli-startup-ld08jvvn`，S3 证据目录是 `tmp/live-cli-startup-te_wj73z`，具体结果为各目录中的 `engineering-repeat.json`。

| 处理器 | 实际失败与原输入重试 | 当前输入重跑及内容验证 |
|---|---|---|
| geometry.export | 实际 CadQuery worker 因测试自有外部条件不可用而失败；恢复条件后，同根 attempt 2 使用完全相同的封存文件成功导出 | 修改当前几何脚本，在新根执行；实际重新导入生成的 STEP，实体体积从 1 变为 8 |
| mesh.generate | 实际流程先完成几何并明确确认 B，再在私有输出位置创建真实冲突文件；blockMesh/checkMesh 执行后因 FileExistsError 失败；新尝试以原输入生成 400 单元网格并发布 | 修改当前 blockMeshDict，新根实际生成 100 单元网格；读取实际 checkMesh 日志与质量报告验证 |
| latex.build | 在私有 PDF 输出位置创建真实冲突文件；真实 pdflatex 编译后，worker 因独占写入冲突失败；原封存重试正常编译并发布 | 修改当前 TeX，新根生成实际新文字 PDF；旧 PDF 和旧源码仍能通过历史发布接口读取 |

每类均验证原封存文件相等、同根重试、新根重跑、正式发布、同 key 不重复执行及旧失败/不可变版本保留。故障只在独立测试应用及自己的运行目录制造，没有替换模型输出、进程退出结果或原生结果。网格 100 单元是当前输入变更的重跑算例，不作为原 400 单元任务的性能替代。

## 旧固定 argv 请求的显式重新准备

`GET /jobs/{id}/operations` 新增 `reprepare`：只有原请求、原封存、归属和终态停止/写入证据有效，且尚未绑定注册处理器时，才提供与原作业类型对应的正式处理器选项。选项表示已注册的处理器；实际运行环境、输入和前置成果仍由正式准备检查。

用户明确选择后，继续使用 `POST /jobs/{id}:repeat`，但 action 为 `rerun`，带当前修订、当前事件序号、正式处理器参数以及：

```json
"reprepare_handler": {"key": "geometry.export", "version": "1"}
```

该操作按当前输入创建新根，重新经过正式配额、输入封存、原生准备、发布和写入复核，不运行旧 argv，也不将当前字节充当历史输入。新尝试的 operation 记录保存源 Job/attempt、输入/输出哈希及选择的正式处理器；旧失败保留，原工作流不推进。旧请求不支持通过这个字段 retry 或 resume，已绑定正式处理器的请求也不能借此替换处理器。

文档原生准备保持在 SQL 锁外执行，回来后核对源请求记录。缺少封存或停止证明的历史记录仍返回恢复要求，不能凭终态字符串启动新执行。新字段未提供或为 null 时，路由不将其加入原请求摘要，保持此前幂等键的请求形状。

FS 证据为 `tmp/live-cli-startup-bdd0usjx/legacy-reprepare.json`；S3 证据为 `tmp/live-cli-startup-pp7snauw/legacy-reprepare.json`。`4jcqi9_l` 为参数归一化修订前的先行 FS 版本，单独统计。

隔离夹具通过正式执行器实际运行固定 argv 的 Python 程序，使用旧 geometry/convert 分类；这是旧类型与停止协议的代表请求，不声称原请求已经执行过 CAD 或 TeX。随后实际运行正式 CadQuery 导出及 pdflatex，分别验证新 STEP 体积为 8、新 PDF 含当前源码文字。每个旧 argv 的执行标记始终只有一条。

两类还验证运行中拒绝、retry/resume 拒绝、错误类型/处理器参数拒绝、当前修订变化拒绝、同 key 重放、源关系与旧失败保留，以及丢失写入排空证明后的拒绝。无 attempt 的旧导入行属于明确标注的协议负例，不能当作原生执行证据。刻意破坏证明的算例保留阻断状态，真实模型回读使用独立干净算例。

## 服务端工具参数

通用重跑和旧请求重新准备现在为 CalculiX、SU2、ParaView 使用各自正式提交入口的当前工具配置；客户端可以只提供公开业务参数。SU2 接受公开的 `config_path`，ParaView 缺省 backend 为 pvpython。兼容与当前配置一致的旧 executable 参数，拒绝指定其他程序。

最终 FS 运行包含三类参数协议检查：实际读取当前配置的二进制并记录 SHA，验证服务端填充、旧参数兼容和替换程序拒绝。这不是 CalculiX/SU2/ParaView 的原生重试验收；三类原生处理器仍在后续清单。

## 证据范围与剩余工作

四个主要成功运行是 FS/S3 工程三类、最终 FS 旧请求及 S3 旧请求，共 8 次真实 `api.deepseek.com / deepseek-flash` Agent 工具回读、34,456 reported tokens。先行 FS 旧请求另 2 次、8,568 tokens；本批合计 10 次、43,024 tokens，模拟调用 0，无失败运行。模型回读绑定各场景报告的哈希，不宣称模型选择了重试、重跑或重新准备，也不代替原生文件内容或协议检查。

FS 工程运行早于旧请求 API 扩展，S3 工程及 S3 旧请求运行早于公开工具参数归一化；最终 FS 旧请求覆盖更新后的 API 与三个工具配置协议。分项证据保留对应时点，最终整体验收仍需绑定最终源码。

所有 API（含测试对等实例）已退出，五个临时 PostgreSQL 库已删除，两套 S3 服务已停止，开发数据库未修改。归档及验证清单为 `tmp/mainline-N5-engineering-and-legacy-evidence/validation.json`。

注册处理器仍为 26 类，其中 13 类有部分命名重试/重跑/续算证据，13 类尚缺：CalculiX、Flotherm 库/转换、MPI、ParaView、两类后处理、三类报告、SU2、两类 UDF。尚缺证据不等于功能没有实现。其他适用模式、工具链/来源变化、旧类型/版本/归属矩阵及既定崩溃交错继续；本批不将 N5-05 或其他主线整项计完成。
