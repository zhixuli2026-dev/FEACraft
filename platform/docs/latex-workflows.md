# LaTeX 注册编译、版本与源码定位

作业取消后，经持久停止证明和尝试绑定回收私有编译目录。Windows 文件共享冲突以及作业所有者退出均已验证重启可接续。[停止后的副本回收](latex-compile-cleanup.md)

绝对文件名现在可显式绑定到已冻结的算例路径。`input_aliases` 适用于宏、Lua 和 biber 读取。完整请求和本机验收见[绝对路径输入冻结](latex-absolute-resources.md)。

后续SyncTeX只读请求也已接入锁外NativeRequest，历史映射/旧版来源重验、HTTP断线与实际退出、viewer权限复核通过双库各25项；继续其余探测和会话入口，详见 [owned-synctex-requests.md](owned-synctex-requests.md)。

后续正式整合：报告/LaTeX提交及工程报告原生准备已移出case写事务；HTTP断线、幂等和来源重验通过本批隔离验证，详见 [document-admission.md](document-admission.md)。原有正式作业、独立内容验证与发布协议继续适用。

原生生命周期增量：worker探测/编译及API发布前独立复编译均由API持有，登记后放行、结束后核验并保存子执行台账，专项双库各15项与本页32项完整回归通过。准备探测、SyncTeX请求与完整恢复仍待续，见 [owned-native-children.md](owned-native-children.md)。

日期：2026-11-02。N3-06 第三批实现记录；SQLite/PostgreSQL各32项通过，具体日志见tmp/mainline-N3-latex-evidence。本文不关闭主线 53 项或全功能 295 项中的未验收工作。HTML/DOCX 与独立组稿分别见 [report-workflows.md](report-workflows.md) 和 [report-compositions.md](report-compositions.md)。

## 编译入口和版本边界

所有 `POST /api/v1/cases/{id}/latex:build` 都使用 `latex.build@1`，包括没有工程后处理配置的普通文档。固定 worker 调用 WSL 中的 latexmk；recipe 只接受 `xelatex`、`pdflatex`、`lualatex`。请求不能传命令、worker 路径或任意编译参数。

```json
{
  "main": "E-report/latex/main.tex",
  "recipe": "xelatex",
  "resources": ["E-report/shared/custom-data"]
}
```

`resources` 可省略，最多 64 个算例内文件或目录。支持 `Idempotency-Key`；相同用户、算例、键与规范化请求返回原 Job，参数冲突返回 409。返回 `job_id/status/pdf/publication_id/generation`，后续通过 Job API 查询和取消。重放不会重新编译或覆盖当前文件。

主文件名支持中文、空格和前导短横线；TeX 不支持的替换字符及反引号、引号等在创建 Job 前拒绝，避免把文件名误解释为命令内容。

每个主文件使用独立的持久发布通道 `latex:<规范化主路径的 SHA-256>`。相同主文件的 recipe 共享通道；不同主文件互不覆盖。通道、代次和当前指针存入数据库，不依赖原 `_LATEX_LATEST` 进程内字典。

已配置工程后处理时，沿用当前证据门禁。已经建立 postprocess head 的算例必须绑定当前同 run 后处理发布、网格质量发布及其递归上游；失效 head 不能通过旧兼容入口绕过。普通独立文档不要求虚构求解或后处理成果。

## 实际支持的输入范围

准备阶段封存主文件所在目录的完整源码子树、算例中的常见 TeX/图片/字体/参考文献/数据文件，以及显式 `resources`。同时冻结原始 caseDict 和报告门禁依赖。默认跳过 `.vibeflow`、`build`、`out`、组稿/LaTeX 历史及临时请求目录。显式选择可包含其他文档的输出文件，但不允许本主文件的生成产物成为自身输入。

宏生成的 `\input`、相对路径、共享图片、biber/BibTeX 以及 Lua 读取的自定义资源在这个封存环境内求值。自定义扩展名位于主目录外时，应明确列入 `resources`。无法在该环境中找到的动态输入会编译失败；不在编译时偷偷回读正在编辑的算例目录。

所有选择沿用 attempt 的路径检查、无链接/硬链接约束，以及 256 项选择、10000 个文件、1 GiB 上限。系统 TeX 包、字体及明确的系统运行时配置单独记录实际读取路径和哈希；不把用户主目录或任意绝对路径视为系统依赖。

## 原生执行和内容验证

worker 在独立 attempt 中另建固定长度 UUID 临时目录，执行 `latexmk -no-shell-escape -norc -synctex=1 -recorder`。每次编译有独立 TEXMFHOME/TMPDIR、固定 SOURCE_DATE_EPOCH 和 Perl 哈希种子；原生日志和系统调用记录分别有预算。固定 Perl 种子用于消除已观测到的 biber 相邻 INFO 行顺序变化。

原生编译的 120 秒截止时间保留旧退出码 124，经 WSL 和宿主 worker 传回 Job；超时诊断带 `native_compile_timeout`。单次验证中重复打开的依赖只做一次路径解析与哈希，独立复编译仍重新采集整份依赖证据。

除 `.fls` 外，使用 `strace` 捕获实际文件打开及网络连接，覆盖 Lua 文件读取等 TeX recorder 没有完整记录的依赖。非封存算例资源、未允许的外部文件、网络读取记录和被冻结输入被修改都会阻止发布。运行时工具路径、版本、可执行文件哈希和实际读取的系统文件哈希写入工具链记录，并在编译后复核。

这是一套依赖与结果验证机制。它不构成任意恶意 TeX/Lua 的操作系统安全沙箱；强制进程隔离、Windows/WSL 全进程树取消和崩溃恢复仍属于 N4/N5。

系统包没有复制为完整操作系统快照，文件存在性、时钟和其他进程状态也没有全部隔离。读取记录约束的是实际捕获到的文件访问；依赖随机数或原生时钟、导致两次编译内容不一致的文档会被拒绝发布。该边界不能解释为任意本机程序都已具备完整可复现环境。

收尾阶段从原封存输入独立再编译，并检查：

| 对象 | 验证方式 |
|---|---|
| PDF | 遍历完整可达对象、解码流、字体/图片/链接/注释与 Info；只忽略容器偏移、压缩和 trailer ID 的差异 |
| SyncTeX | 核对完整位置记录和源码映射，仅规范化独立临时目录前缀 |
| BBL | 有引用时必须存在且非空，与独立编译逐字节一致 |
| 日志 | 只规范化临时目录前缀，其他内容要求一致 |
| 源码副本 | 实际读取的算例源文件必须对应原封存字节 |
| 清单/工具链 | 校验输入、产物、case/job/attempt/execution、代码哈希及原生依赖记录 |

因此空命令、伪造 PDF、同步映射、BBL 或修改后重算清单都不足以生成有效发布。PDF 比较限制为 1–1000 页、100000 对象、256 MiB 展开流；SyncTeX 展开限制为 64 MiB。

## 发布、历史和工作副本

不可变 bundle 保存 PDF、原始 SyncTeX、可选 BBL、日志、manifest/toolchain，以及 `E-report/latex-history/{execution_id}/sources/` 下实际读取的源码。只有当前有效代次且源与上游仍匹配的作业才能切换 head。迟到的有效版本保存为 superseded 历史；失败、取消或源变化不能替换上一份 PDF。

工作副本仍写到主文件目录的 `build/{stem}.*`，PDF 最后写入。新版本没有参考文献时，只删除仍与旧发布字节一致的旧 BBL；人工修改的 BBL 保留，并记录镜像尚未应用。源码历史只存在于不可变 bundle，不覆盖用户编辑的源文件。

三个状态必须分开读取：`publication.current` 表示当前数据库指针；`publication.stale` 表示源/上游/当前性失效；`publication.aliases_applied` 表示工作副本与该版本产物一致。Job succeeded 不能替代这三项。提交后的镜像失败保留不可变版本，记录 `artifact.alias_repair_required`；完整 FS/DB 崩溃对账仍待 N5。

| 接口（算例前缀下） | 行为 |
|---|---|
| GET `/latex/results?main=...` | 读取该主文件当前版本，默认主文件为 `E-report/latex/main.tex` |
| GET `/latex/results?publication_id=...` | 显式读取历史版本及 manifest/toolchain |
| GET `/latex/publications?main=...` | 最近 50 条发布记录；可省略 main 查询算例所有主文件 |
| GET `/latex/artifact?path=...&publication_id=...` | 只下载此发布列出的 PDF/SyncTeX/BBL/日志等产物 |
| GET `/latex/source?path=...&publication_id=...` | 按原算例路径读取该版本保留的源文件 |

跨用户、跨算例、错误发布类别或未列出的路径返回 404；不可变证据损坏返回 409。当前指针失效不删除历史。

## SyncTeX 与诊断

`POST /synctex:query` 保留原 `pdf/page/x/y` 参数，新增可选 `publication_id`。有注册 head 时读取不可变 PDF 与原始侧车文件；不改写 SyncTeX Input 行，以免破坏字节偏移锚点。工具链记录原临时路径到算例源路径的映射，因此临时目录清理后仍能定位。

响应保留 `file/line/column/page`，增加 `publication_id/source_sha256/source_current_matches/publication`。历史源与当前编辑文本可能不同；应通过 `/latex/source` 查看对应版本，而不是把历史行号当作当前文件的确定位置。没有注册 head 的旧 PDF 继续使用原兼容查询。

`/latex/errors?job_id=...` 将原生临时路径映射回原源码路径。发布内容验证抛出的错误另保存在 attempt 输出的 `publication_error` 并进入诊断列表；源变化等状态冲突还可从 attempt 的 reason_code 查询。不把“编译退出 0、内容验证失败”伪装成成功。

## 验收和剩余工作

`server/latex_handler_smoke.py --postgres` 在隔离 SQLite/临时 PostgreSQL 中测试三种真实编译器、biber、动态依赖、不可变源/产物、SyncTeX、权限、双 API 幂等、读取端重启、篡改、来源变化、并发与取消，以及真实工程后处理消费。临时测试模块提供故障注入，不在产品 API 中增加测试开关。最终双库各32项；HTML/DOCX各23项、compose各25项；core14/14、契约4/4、旧兼容4/4、最终原生兼容3/3通过。旧生命周期真实120秒超时保留exit124。未重跑完整offline/writes、前端或CAE全集；本轮工程消费使用真实导热工作流。

下一批先统一无 postprocess head 的普通 HTML/DOCX 和 compose 兼容发布路径；随后按主线推进进程身份和进程树终止、运行中 owner 失联恢复、发布事务对账及版本 UI。N4 的隔离能力原型已在主线计划记录，但没有提前登记为应用级完成。全功能计划继续覆盖更多报告模板、复杂资源、S3/多副本与恢复验证。不会因为本地 PDF 已生成就关闭这些任务，也不迁移或重载开发实例。

注册套件已列入 `run_regression.py --profile offline`，每个成功候选都会真实编译并独立复验，单库整套默认超时为 1800 秒；其他套件仍默认 600 秒，显式 `--timeout` 可覆盖。这里的套件超时与产品原生单次编译的 120 秒截止时间不同。
