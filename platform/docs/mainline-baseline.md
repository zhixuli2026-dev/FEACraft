# N0 源码基线与恢复验收

启动登记与重启核对基线 `platform/backups/mainline-N5-launch-registration-20261103.zip`；SHA见同名.sha256，逐文件匹配见tmp/mainline-N5-launch-registration-evidence/current-source-match.json。只含白名单源码，未恢复后重跑；N5-03及完整53/295目标继续。

历史Job排空恢复基线 `platform/backups/mainline-N5-job-drain-recovery-20261103.zip`；SHA见同名.sha256，逐文件匹配见tmp/mainline-N5-job-drain-recovery-evidence/current-source-match.json。只含白名单源码，未恢复后重跑；其余登记/收尾/文件恢复继续。

发布来源校验基线 `platform/backups/mainline-N5-publication-source-20261103.zip`；SHA见同名.sha256，逐文件匹配见tmp/mainline-N5-publication-source-evidence/current-source-match.json。只含白名单源码，未恢复后重跑；旧记录和其余长事务恢复继续。

封存准入与读取/取消基线 `platform/backups/mainline-N5-validation-access-20261103.zip`；SHA见同名.sha256，逐文件匹配见tmp/mainline-N5-validation-access-evidence/current-source-match.json。只含白名单源码，未恢复后重跑。真实发布接线和旧历史恢复继续。

Job写入排空与来源校验准入基线 `platform/backups/mainline-N5-validation-job-20261103.zip`；SHA见同名.sha256，逐文件匹配见tmp/mainline-N5-validation-job-evidence/current-source-match.json。仅白名单源码，不含开发数据/私有配置；未恢复后重跑。真实发布接线与旧历史恢复继续。

原生会话与来源校验互斥基线 `platform/backups/mainline-N5-validation-native-20261103.zip`；SHA见同名.sha256，逐文件匹配见tmp/mainline-N5-validation-native-evidence/current-source-match.json。仅白名单源码，不含开发数据/私有配置，未恢复后重跑；Job写入和正式发布接线继续。

来源校验占用基础协议基线 `platform/backups/mainline-N5-case-validation-20261103.zip`；SHA见同名.sha256，逐文件匹配见tmp/mainline-N5-case-validation-evidence/current-source-match.json。只含白名单源码，不含开发数据/私有配置，未恢复后重跑；真实发布接线和原生写者保护继续。

上传线程排空基线 `platform/backups/mainline-N5-upload-coordination-20261103.zip`；SHA见同名.sha256，逐文件匹配见tmp/mainline-N5-upload-coordination-evidence/current-source-match.json。仅白名单源码，不含开发数据/私有配置，未恢复后重跑。

发布最终校验与终端活动基线 `platform/backups/mainline-N5-publication-verify-20261103.zip`；SHA见同名.sha256，逐文件匹配见tmp/mainline-N5-publication-verify-evidence/current-source-match.json。仅白名单源码，不含开发数据/私有配置，未恢复后重跑。

发布包锁外准备基线 `platform/backups/mainline-N5-publication-copy-20261103.zip`；SHA见同名.sha256，逐文件匹配见tmp/mainline-N5-publication-copy-evidence/current-source-match.json。仅白名单源码，不含开发数据/私有配置；未恢复后重跑。

收尾I/O基线 `platform/backups/mainline-N5-finalizing-io-20261103.zip`；SHA见同名.sha256，逐文件匹配见tmp/mainline-N5-finalizing-io-evidence/current-source-match.json。仅白名单源码，不含开发数据/私有配置；未恢复后重跑。

启动续租与锁外校验基线 `platform/backups/mainline-N5-launch-lease-20261103.zip`；SHA见同名.sha256，逐文件匹配见tmp/mainline-N5-launch-lease-evidence/current-source-match.json。仅白名单源码，不含开发数据/私有配置；未恢复后重跑。

Job实例身份与启动对账基线 `platform/backups/mainline-N5-job-owner-20261103.zip`；SHA见同名.sha256，逐文件匹配见tmp/mainline-N5-job-owner-evidence/current-source-match.json。仅白名单源码，不含开发数据/私有配置；未恢复后重跑。

原生子执行放行基线 `platform/backups/mainline-N4-native-release-20261103.zip`；SHA见同名.sha256，逐文件匹配见tmp/mainline-N4-native-release-evidence/current-source-match.json。仅白名单源码，不含开发数据/私有配置；未恢复后重跑。

取消与启动门顺序基线 `platform/backups/mainline-N4-job-release-20261103.zip`；SHA见同名.sha256，逐文件匹配见tmp/mainline-N4-job-release-evidence/current-source-match.json。仅白名单源码，不含开发数据/私有配置；未恢复后重跑。

FloTHERM复制编译基线 `platform/backups/mainline-N4-flotherm-copy-20261103.zip`；SHA见同名.sha256，逐文件匹配见tmp/mainline-N4-flotherm-copy-evidence/current-source-match.json。仅白名单源码，不含开发数据/私有配置；未恢复后重跑。

原生编译所属通道基线 `platform/backups/mainline-N4-native-channels-20261103.zip`；SHA见同名.sha256，逐文件匹配见tmp/mainline-N4-native-channels-evidence/current-source-match.json。仅白名单源码，不含开发数据/私有配置；未恢复后重跑。

所属只读身份探测基线 `platform/backups/mainline-N4-control-readers-20261103.zip`；SHA见同名.sha256，逐文件匹配见tmp/mainline-N4-control-readers-evidence/current-source-match.json。仅白名单源码，不含开发数据/私有配置；未恢复后重跑。

专业求解器及worker停止基线 `platform/backups/mainline-N4-professional-solvers-20261103.zip`；SHA见同名.sha256，逐文件匹配见tmp/mainline-N4-professional-solvers-evidence/current-source-match.json。仅白名单源码，不含开发数据/私有配置；未恢复后重跑。

专业进程故障基线 `platform/backups/mainline-N4-professional-scopes-20261103.zip`；SHA见同名.sha256，逐文件匹配见tmp/mainline-N4-professional-scopes-evidence/current-source-match.json。仅白名单源码，不含开发数据/私有配置；未恢复后重跑。

所属CLI初始化基线 `platform/backups/mainline-N4-cli-preparation-20261103.zip`；SHA见同名.sha256，逐文件匹配见tmp/mainline-N4-cli-preparation-evidence/current-source-match.json。仅白名单源文件，不含开发数据/私有配置；未恢复后重跑。

专业入口与WSL启动补证基线 `platform/backups/mainline-N4-professional-entry-20261103.zip`；摘要见同名.sha256，当前源码逐文件匹配见tmp/mainline-N4-professional-entry-evidence/current-source-match.json。白名单源文件，不含开发数据/私密配置；未恢复后重跑。

原生启动基线 `platform/backups/mainline-N4-native-startup-20261103.zip`；SHA见同名.sha256，源码匹配见tmp/mainline-N4-native-startup-evidence/current-source-match.json。白名单源码/文档/测试，不含私密配置或开发数据；未恢复后重跑。

原生模型参数基线 `platform/backups/mainline-N4-native-model-20261103.zip`；SHA见同名.sha256，源码匹配见tmp/mainline-N4-native-model-evidence/current-source-match.json。白名单源码/文档/测试，不含私密配置或开发数据；未恢复后重跑。

原生用量基线 `platform/backups/mainline-N4-native-usage-20261103.zip`；SHA见同名.sha256，源码匹配见tmp/mainline-N4-native-usage-evidence/current-source-match.json。白名单源码/文档/测试，不含凭据、个人CLI目录或开发数据库；未恢复后重跑。

原生图片基线 `platform/backups/mainline-N4-native-image-20261103.zip`；SHA见同名.sha256，源码匹配见tmp/mainline-N4-native-image-evidence/current-source-match.json。仅白名单源码/文档/测试，不含图片副本、私有配置或开发数据；未恢复后重跑。

原生CLI分叉基线 `platform/backups/mainline-N4-native-fork-20261103.zip`；SHA见同名.sha256，当前源码完整匹配见tmp/mainline-N4-native-fork-evidence/current-source-match.json。白名单源码/文档/测试，不含开发数据、临时凭据或构建依赖；未恢复后重跑。

统一原生聊天基线 `platform/backups/mainline-N4-native-chat-20261103.zip`；SHA见同名.sha256，当前源码完整匹配见tmp/mainline-N4-native-chat-evidence/current-source-match.json。仅白名单源码/文档/测试，不含开发数据、临时凭据或构建依赖；本批未恢复后重跑。

WSL CLI/sandbox状态基线 `platform/backups/mainline-N4-cli-wsl-20261103.zip`，843白名单源文件/500Python，SHA见同名.sha256；逐文件一致性见tmp/mainline-N4-cli-wsl-evidence/current-source-match.json。包含代码、迁移、配置模板和文档，不包含Linux私有状态、开发数据或凭据。未恢复后重跑；部署与完整交付仍继续。

网站SIWC基线 `platform/backups/mainline-N4-website-auth-20261103.zip`，834白名单源文件/492Python；SHA见同名.sha256，逐文件一致性见tmp/mainline-N4-website-auth-evidence/current-source-match.json。包含迁移、代码、UI、配置模板与文档，不包含开发数据/凭据。未恢复后重跑，正式client仍待联调。

本批托管API凭据基线：`platform/backups/mainline-N4-cli-credentials-20261103.zip`，823白名单文件/486 Python；摘要见同名.zip.sha256，逐文件匹配见tmp/mainline-N4-cli-credential-evidence/current-source-match.json。保存托管决策、凭据隔离/撤销与实际CLI验证；未恢复后另行重跑，不包含开发数据或凭据。网站SIWC/WSL/sandbox/UI和完整53/295目标继续。

本批原生回合基线：`platform/backups/mainline-N4-cli-turns-20261103.zip`，819白名单文件/484 Python，保存持久回合/SSE/审批与共享执行归属；摘要见同名.zip.sha256，逐文件匹配见tmp/mainline-N4-cli-turn-evidence/current-source-match.json。未恢复后另行重跑，开发数据/凭据/依赖不入包；账户/sandbox/WSL/UI及完整53/295目标继续。

本批CLI会话基线：`platform/backups/mainline-N4-cli-sessions-20261103.zip`，808白名单文件/474 Python，保存宿主持久CLI会话、正式API、私有环境、S3收尾和专项。摘要见同名.zip.sha256，逐文件匹配见tmp/mainline-N4-cli-session-evidence/current-source-match.json。未恢复后另行重跑，开发数据/凭据/依赖不入包；g2b2回合/审批/账户/WSL/UI和完整53/295目标继续。

本批g2a通信基础基线：`platform/backups/mainline-N4-codex-transport-20261103.zip`，800白名单文件/467 Python，涵盖所属stdio、实际协议修正、私有空环境及专项。摘要见同名.zip.sha256，逐文件核验见tmp/mainline-N4-codex-transport-evidence/current-source-match.json。没有恢复后另行重跑；开发数据/凭据/依赖不入包。g2b产品持久API仍待，53/295完整范围继续。

此前Agent shell基线：`platform/backups/mainline-N4-agent-shell-20261103.zip`，794个白名单文件、462个Python；摘要见同名.zip.sha256，逐文件当时源码匹配见tmp/mainline-N4-agent-shell-evidence/current-source-match.json。保存API Agent shell归属/原子审批/实际退出及S3收尾；未另做恢复后重跑，开发数据/秘密/依赖不入包，53/295完整目标继续。

此前viz基线：`platform/backups/mainline-N4-viz-20261103.zip`，788个白名单源文件、457个Python文件；摘要见同名.zip.sha256，完整逐文件匹配见tmp/mainline-N4-viz-evidence/current-source-match.json。保存viz归属/首帧就绪/实际退出/端口竞争及S3收尾；未另做恢复后重跑，开发数据/秘密/依赖不入包，53/295完整交付范围继续。

此前PTY基线：`platform/backups/mainline-N4-pty-20261103.zip`，780个白名单源文件、450个Python文件，记录PTY持久归属、实际退出、独立数据管道及2MiB背压、S3清理意图保护和专项。摘要见同名.zip.sha256，逐文件匹配见tmp/mainline-N4-pty-evidence/current-source-match.json；没有另做恢复后重跑。开发数据、秘密及依赖不入包，53/295完整范围继续保留。

此前runtime基线：`platform/backups/mainline-N4-runtime-20261103.zip`，769个白名单源文件、440个Python文件，记录用户所属Python/WSL能力探测、固定用途helper、全局scope迁移及专项。摘要见同名.zip.sha256；逐文件匹配见tmp/mainline-N4-runtime-evidence/current-source-match.json。本批未恢复后另行重跑；开发数据、秘密和依赖不入包，53/295范围继续保留。

此前SyncTeX基线：`platform/backups/mainline-N4-synctex-20261103.zip`，764个白名单源文件，记录所属只读SyncTeX查询、固定快照/环境、共用请求等待和用途约束迁移。摘要见同名.zip.sha256；逐文件匹配见tmp/mainline-N4-synctex-evidence/current-source-match.json。本批未恢复后另行重跑；开发数据、秘密和依赖不入包，53/295范围继续保留。

此前文档准备基线：`platform/backups/mainline-N4-admission-20261102.zip`，757个白名单源文件，记录正式文档/工程报告锁外准备、ASGI断线、来源/资源选择和封存重验。摘要见同名.zip.sha256；逐文件匹配见tmp/mainline-N4-admission-evidence/current-source-match.json。本批未另行恢复后重跑；开发数据、秘密和依赖不入包，53/295范围继续保留。

此前请求基础基线：`platform/backups/mainline-N4-request-20261102.zip`，754个白名单源文件，记录独立准备请求归属服务、迁移和隔离验证。摘要见同名.zip.sha256；逐文件匹配见tmp/mainline-N4-request-evidence/current-source-match.json。业务锁外准备仍待整合；本批未恢复后另行重跑，开发数据/秘密/依赖不入包，53/295范围继续保留。

此前终态一致性基线：`platform/backups/mainline-N4-terminal-20261102.zip`，749个白名单源文件，记录持久终态字段、兼容产物收尾和跨API日志协议。摘要见同名.zip.sha256；逐文件匹配见tmp/mainline-N4-terminal-evidence/current-source-match.json。本批未另行恢复重跑；开发数据、秘密和依赖运行时不入包，53/295范围仍完整保留。

此前Pandoc子执行基线：`platform/backups/mainline-N4-pandoc-20261102.zip`，747个白名单源文件，记录Pandoc所属原生子执行、stdin/断线等待和Windows成员出生读取竞态修复。摘要见同名.zip.sha256，逐文件匹配见tmp/mainline-N4-pandoc-evidence/current-source-match.json；本批未另行恢复重跑。开发数据、秘密与依赖运行时不入包；N4-03/04继续实施。

此前LaTeX子执行基线：`platform/backups/mainline-N4-native-20261102.zip`，743个白名单源文件，记录LaTeX所属原生子执行、持久台账、迁移和验收。摘要见同名.zip.sha256，逐文件匹配见tmp/mainline-N4-native-evidence/current-source-match.json；本批未另行恢复重跑。开发数据、秘密及依赖运行时不入包；N4-03/04继续实施。

此前发布验证基线：`platform/backups/mainline-N4-preflight-20261102.zip`，737个白名单源文件，记录事务外原生验证与持久取消增量。摘要见同名.zip.sha256，逐文件匹配见tmp/mainline-N4-preflight-evidence/current-source-match.json；本批未另行恢复重跑。N4-03/04继续实施，开发数据、秘密及依赖运行时不入包。

此前MPI增量基线：`platform/backups/mainline-N4-mpi-20261102.zip`，735个白名单源文件，记录N4-03首批MPI接线；N4-03整项仍实施中。摘要见同名.zip.sha256，逐文件匹配见tmp/mainline-N4-mpi-evidence/current-source-match.json；本批未另行恢复重跑，不包含开发数据/秘密/依赖运行时。

此前N4-02最终基线：`platform/backups/mainline-N4-lifetime-20261102.zip`，733个白名单源文件，关闭N4-02并进入N4-03。摘要见同名.zip.sha256，逐项匹配见tmp/mainline-N4-lifetime-evidence/current-source-match.json；本批未另行恢复重跑，不包含开发数据/秘密/依赖运行时。

此前竞态验收基线：`platform/backups/mainline-N4-tree-races-20261102.zip`，731个白名单源文件；同名.zip.sha256及tmp/mainline-N4-tree-races-evidence/current-source-match.json记录哈希与逐项匹配。本批补充16项停止竞态/真实编译器验收，执行代码与前批730文件基线一致；N4-02仍实施中。本批没有另建恢复目录重跑。

此前停止实现基线：`platform/backups/mainline-N4-tree-20261102.zip`，730个白名单源文件；摘要见同名.zip.sha256，逐文件匹配见tmp/mainline-N4-tree-evidence/current-source-match.json。此包记录N4-02首批增量，不代表整项完成；本批未另行恢复重跑，开发数据/私有配置/依赖运行时不入包。

此前身份基线：`platform/backups/mainline-N4-identity-20261102.zip`，726个白名单源文件；摘要见同名.zip.sha256，逐文件匹配见tmp/mainline-N4-identity-evidence/current-source-match.json。此前普通报告基线717文件。本次不另建恢复目录重跑；开发数据/私有配置/依赖运行时不入包。

此前普通报告基线：`platform/backups/mainline-N3-ordinary-documents-20261102.zip`，717个白名单源文件；SHA-256见同名.zip.sha256，完整匹配见tmp/mainline-N3-ordinary-document-evidence/current-source-match.json。前版LaTeX基线715文件；本次逐文件校验，未另作恢复目录重跑，开发数据/私有配置/依赖运行时排除。

此前LaTeX增量基线：`platform/backups/mainline-N3-latex-20261102.zip`，715个白名单源文件；SHA-256见同名.zip.sha256，完整匹配见tmp/mainline-N3-latex-evidence/current-source-match.json。前版mainline-N3-compose-20261102.zip（708文件）。本次完整哈希校验，未另作恢复目录重跑；开发数据/私有配置/运行时均排除。

历史compose增量基线：`platform/backups/mainline-N3-compose-20261102.zip`，包含708个白名单源文件；SHA-256见同名.zip.sha256，逐文件当前源码匹配见tmp/mainline-N3-compose-evidence/current-source-match.json。前一版为mainline-N3-report-html-20261102.zip（702文件）。本版未再恢复到新目录重跑；开发数据与运行时继续排除。

工作区没有 Git 历史。本阶段使用带 SHA-256 清单、独占创建和独立恢复演练的源码归档，保留可回退版本；不把历史审查 diff 当作已提交版本。生成工具为 `tools/source_baseline.py`，`.zip.sha256` 验证归档整体，归档内 `source-baseline.json` 验证每个文件并记录 Python/依赖包版本。首次版本遗漏 Dockerfile 后缀，被恢复后的 compose 检查发现；v2 补齐部署模板，v3 另外明确排除隐藏浏览器配置目录。旧归档只保留本地历史，不作为最终可交付基线。

归档包括 platform 产品源码、文档、测试、配置模板、技能静态资产、补全静态资产和 `.github` 工作流。排除所有 `.env`（仅保留 `.env.example`）、用户 data、vault、浏览器 profile、虚拟环境、node_modules、构建产物、日志、备份与本地基础设施二进制。工具不复制真实用户场文件；恢复用户数据属于另行的备份恢复流程。依赖包、OpenFOAM/WSL、Pandoc/TeX/ParaView 等运行时由当前已验证环境提供，归档不是离线安装镜像。

命令在项目根运行：

```powershell
& platform/server/.venv/Scripts/python.exe -B platform/tools/source_baseline.py create platform/backups/mainline-N0-20261102-v3.zip
& platform/server/.venv/Scripts/python.exe -B platform/tools/source_baseline.py verify platform/backups/mainline-N0-20261102-v3.zip
& platform/server/.venv/Scripts/python.exe -B platform/tools/source_baseline.py restore platform/backups/mainline-N0-20261102-v3.zip --destination tmp/mainline-N0-restored-v3-20261102
```

create 不覆盖同名版本；restore 仅接受全新目录，先校验再创建，不能覆盖现有工作区。回退时从归档恢复到新目录、安装/关联锁定依赖并在临时 DB/fs 验证，再由操作者切换启动路径；不直接解包覆盖现有代码/用户目录。没有做现有开发库迁移或用户数据恢复。

恢复后分别执行该目录下 `platform/tools/run_regression.py --profile offline/core/cae`，使用原已验证解释器，但代码、API app-dir、临时数据库、文件和索引必须来自恢复目录。日志置于项目 `tmp/mainline-N0-baseline-v3-evidence/`；不连接原开发 PostgreSQL，不停止原8000服务。三条 CAE 使用真实 CadQuery、OpenFOAM v2406、VTK 和 Pandoc，仍保留线性导热误差、二维/三维空腔残差与场的硬断言。前端另在恢复目录关联现有 node_modules 后执行 build 和既有 Node 纯检查；关联仅用于本地恢复演练，不收入归档。

后续里程碑产生新的版本归档与验证记录，不覆盖 N0。如果依赖/运行时变更，先更新版本证据和恢复命令；归档完整性通过不等于运行验收通过。任务关闭记录保存在 `docs/mainline-task-status.json`，以实际日志为准。
