# 校验占用期间的内容写入、读取与取消

2026-11-03，N5-01增量。输入封存新增校验占用准入；已核对的结果读取与批量取消不再被误当作内容写入。主线仍为53项中的11完成、18进行中、24待开始，剩42项；295项全功能尚未按完整交付条件销项。本单元不关闭N5-01，实际发布来源/依赖校验尚未使用占用协议。

## 问题与修复

修复前的双API复现见 `tmp/validation-access-before-n6rzfek9/before-result.json`，脚本绑定上一版源码归档。持有校验占用时，真实Job提交入口先封存输入并提交Job，然后在claim处拒绝，已经留下输入文件和数据库记录。与此同时，几何读取、批次详情/列表和批次取消都被占用检查拒绝。复现使用明确标注的无原生执行校验者与暂停派发的排队批次，没有放行作业命令。

`attempt_workspace.seal` 现在在调用者持有的case控制锁内，先检查占用，再检查或复制来源。普通提交、发布输入提交和复用旧输入的重试共用此入口。冲突由原有savepoint回滚Job、attempt、head、请求等改动；在进入文件操作前拒绝，因此不会留下封存目录。幂等重放仍优先返回已有记录。这里没有将封存的复制I/O移出事务，也没有实现未知提交后的孤儿目录恢复。

以下21处协调段指定 `content_write=False`，涉及21个GET入口及1个批量取消入口。它只区分内容读取与写入，原有case控制锁、可用性、身份权限和结果校验继续生效；未改动路由的其他逻辑。

| 入口组 | 已核对的读取段 |
| --- | --- |
| 几何 | `_document`供scene/validation使用；publications |
| 网格、求解、后处理 | 各自的results与publications |
| 报告草稿 | compose-result、compositions、compose-artifact |
| 报告构建 | results、publications、artifact |
| LaTeX | results、publications、artifact、source |
| 批量作业 | 详情、列表，以及取消前读取清单/作业行的协调段 |

批量取消读取结束后提交该事务，逐项状态转换继续使用独立case事务。取消不产生新的内容写入；活动原生写者仍由已有生命周期检查阻止授予校验占用。不能用这项修改允许活动写者与校验并行。结果读取仍执行不可变文件哈希、归属、类型、当前版本和必要的来源校验，损坏报告仍返回evidence_invalid。

## 其余直接调用的分类

当前AST辅助清单覆盖241个app Python文件；它识别直接调用，包含字符串replace等误报，不能代替完整动态调用图。结合调用者阅读得到以下分类，不宣称已覆盖任意扩展代码或外部进程。

| 类别 | 当前边界与后续限制 |
| --- | --- |
| 文件、分块上传、caseDict、几何修复/预览、网格质量、后处理准备、专业工具、Agent提案 | 正式入口经coordinate；上传取消先排空后台写线程。部分批量I/O仍持有数据库锁。 |
| Job私有输入、工作副本、日志、回写与发布包 | 本次补齐seal；claim、原生放行与写入排空检查点见validation-job-admission。封存/别名/暂存的恢复仍待完成。 |
| 终端、CLI、Agent执行、可视化、原生准备请求 | 新准入与持久会话停止事实见validation-native-admission；未知停止不能由租约过期替代。 |
| 工作流创建、读取、确认准备、调度与取消 | 单纯计划/状态操作写DB；真正内容改动经coordinate，排队封存经seal，原生准备经NativeRequest。未给整个locked_run增加写入拒绝。 |
| 复制、导入、恢复 | 对已有来源/目标的操作保留协调；新case使用尚未提交的唯一前缀及既有补偿。旧恢复Job缺乏排空证明仍需单独处理。 |
| 分享、运行配置、密钥、审计、搜索索引、技能及全局OpenCode配置 | 使用case前缀之外的独立路径，不当作case内容写入；不据此豁免各自权限或恢复要求。 |
| 旧report_build/latex_build快照发布辅助函数、旧mesh gate辅助函数 | 当前app未找到正式调用者；保留静态结论，不把未调用代码算成已验证功能。 |

实际发布的完整来源/递归依赖检查、结果读取中的长事务，以及封存/别名的写入恢复还未完成。同版本应用协议不等于OS租户隔离；外部程序直接修改文件的情况仍需完整哈希验证和部署边界。

## 验证与证据

`validation_access_smoke.py` 在SQLite/PostgreSQL各通过16项，使用共享数据库和存储的两个API实例。比较全部case文件内容哈希和目录，包括空目录；比较Job、attempt、head、请求记录。覆盖路径/发布输入提交、服务级重试、幂等重放、held与过期held、21个读取入口、权限、错误路径、损坏产物、续租、心跳、无关case、排队取消、精确清理与恢复写入。

报告来自真实Pandoc构建并验证下载字节；普通封存作业使用实际Windows进程并核验停止回执。几何使用旧格式结果读取，其余专业发布类型只覆盖空结果、列表和错误选择路径，不冒充完整专业成果验收。重试通过测试专用入口调用生产服务，未增加公开重试API。排队批次由真实API创建，夹具只暂停派发；活动批次的执行/取消由既有batch_jobs_smoke独立验证。

最终双库结果为 `tmp/validation-access-evidence-gszhvf7f/result.json`，临时PostgreSQL库已删除。兼容检查包括：case_validation_smoke的SQLite32项、attempt_workspace_smoke的SQLite23项（实际Windows/WSL及CadQuery）、batch_jobs_smoke的一套真实运行/取消回归。原兼容批次结果为1/2，封存套件遇到受限环境的WSL E_ACCESSDENIED；诊断确认失败发生在WSL启动握手，原始与诊断日志保留，授权环境下单套复验通过，没有放松产品握手检查。

源码AST比对确认6个路由文件只有上述21处参数变化；规范证据索引位于 `tmp/mainline-N5-validation-access-evidence/`。测试注册all270/offline98/writes30；schema仍为head6a4fc1b5e087、36张业务表。本轮没有迁移开发数据库或重载端口8000，正式SIWC联调按用户决定暂缓。
