# Linux TeX 与 SyncTeX 部署

2026-11-18。正式 `latex.build`、构建前工具探测和冻结 PDF 的 SyncTeX 查询按宿主系统选择执行方式。Windows 保留原 WSL 发行版，Linux 使用容器内的实际 TeX 工具；注册作业、输入快照、独立重编译校验、发布和停止凭据继续复用既有流程。

## 构建与运行

在 `platform` 目录构建。单独文档服务可直接以基础 API 镜像为底层；需要工程求解和文档的部署先构建 CAE 镜像，再加 TeX。

```sh
docker build -f infra/Dockerfile.api -t vibeflow/api:dev .
docker build -f infra/Dockerfile.cae --build-arg API_IMAGE=vibeflow/api:dev -t vibeflow/api:cae .
docker build -f infra/Dockerfile.tex --build-arg API_IMAGE=vibeflow/api:cae -t vibeflow/api:tex .
docker compose --env-file infra/.env -f infra/docker-compose.prod.yml -f infra/docker-compose.tex.yml --profile distributed up -d
```

若使用 Compose 构建覆盖文件，设置 `VIBEFLOW_API_IMAGE=vibeflow/api:cae` 选择已构建的底层镜像；`VIBEFLOW_TEX_IMAGE` 指定最终镜像。不要把 CAE 和 TeX 两份覆盖文件直接叠加，当成自动串联的镜像构建。分布式调度仍需 `VIBEFLOW_DISTRIBUTED_JOBS=true`，密钥和数据卷按 [部署说明](deployment.md)配置，API/worker 使用相同镜像。

Dockerfile 默认安装 latexmk `1:4.86~ds-1`、texlive-xetex/luatex `2024.20250309-1`、texlive-latex-extra/bibtex-extra `2024.20250309-2`、biber `2.20-2`、Noto CJK `1:20240730+repack1-1`，以及 fontconfig 和 strace。版本可通过 Compose 的同名构建参数更换；更换后需复验目标文档。字体缓存和 Lua 字体索引在镜像构建时初始化，避免首个用户编译过程中创建未稳定的全局缓存。

Linux 工具链记录 `distro=native-linux`。普通 TeX 构建不需要 OpenFOAM bashrc；已有正式工程结果的算例仍会核验其工程来源。固定参数继续包括 `-no-shell-escape`、`-norc`、`-synctex=1` 和 `-recorder`，真实 strace 文件访问与 `.fls` 输入记录共同核验依赖。Debian libpaper2 的 `/etc/paperspecs` 作为具体系统配置逐文件记录哈希，不开放整个 `/etc`。

LuaLaTeX 的可写字体缓存使用相对路径 `out/texmf-cache`，读取缓存的后备目录为 `/var/lib/texmf`。字体加载器的写入探测也留在本次编译输出目录，不能把整个系统缓存目录当成允许输出的位置；绝对缓存路径会被 TeX 的写入限制拒绝，因此必须保持相对路径。独立原生复现保留了实际 `.fls` 和 strace 输出位置。WSL 复验环境补装了原先缺失的 Ubuntu `texlive-luatex=2023.20240207-1`，普通编译仍使用 nobody。

这个缓存目录在每次编译前新建且为空。Lua 生成的字体字节码与索引具有非稳定序列化，不作为输入源逐字节比较；访问过的缓存文件相对路径记录为 `volatile_runtime_inputs` 中的 `@lua-font-cache/…`。真实字体、读取的系统缓存及其他系统输入仍逐文件哈希，路径解析后越出该私有缓存目录仍走原有依赖校验。独立重编译继续比较完整 PDF 语义图、SyncTeX、日志、参考文献、冻结源码和输入清单。

## 验收记录

当前阶段证据位于 `tmp/linux-tex-20261118`。Windows/WSL 与 Linux/S3 的 pdfLaTeX 已实际生成双页 PDF 和 BibTeX 参考文献，核验文字、PDF/产物哈希、冻结源码、SyncTeX 坐标及版本约束的分段下载。Linux XeLaTeX 的中文字体、biber 参考文献及 LuaLaTeX 也已通过同一真实流程，Windows/WSL LuaLaTeX 兼容复验通过。随后修改实际源码，核验旧 PDF 过期但冻结源码与定位保持；恢复原文后重新一致。实际重建 API/worker，三份历史 PDF 哈希和 SyncTeX 定位保持。共 5 个成功文档作业；作业子进程及准备/查询请求停止凭据分别核验。未通过的首次结果保留。

首次 Windows 定位命中了生成的 `.bbl`：测试文档的输入段落未结束，TeX 把段落位置归入参考文献。补上真实段落结束后通过，没有修改源码映射规则。Linux XeLaTeX 首次暴露缺少预生成字体缓存；后续暴露新版 libpaper2 的具体纸张配置依赖，均据实际编译记录修正。

验收用 SeaweedFS 曾因 `-master.volumeSizeLimitMB=32`、`-volume.max=3` 用尽可写卷而写入超时，容器仍显示 healthy。已保留数据，将该验收部署上限增加至 32 卷。该故障不是磁盘已满，也不是 Docker 引擎停止；容器健康标志不能证明 S3 仍可写。原日志、扩容记录和提交失败均保留。

源码过期复验使用最后生成的 LuaLaTeX 发布。较早的 pdfLaTeX 发布之后，同一算例新增了另外两种 recipe 的 `.tex`/`.bib`，这些属于现有算例级资源快照，因此即使恢复它自己的 `section.tex`，它仍应保持过期；旧发布的文件哈希读取不受影响。首次部署检查误把旧发布当成当前资源基线，失败结果保留，后续在当前完整资源基线上验证源编辑与精确恢复。

## 更新、恢复与边界

更新前停止提交并等待作业结束，保存镜像标识、配置、数据库和存储；再更换 API/worker 镜像并等待 `/api/health` 正常。旧发布保留其工具链和输入快照，回退时保留对应工具版本和数据；不要删除存储卷。源码改变后旧 PDF 可继续按发布编号读取，但应标记过期，SyncTeX 应返回冻结源码及其与当前源码是否一致。

本次正常发布的独立重编译属于当前作业执行范围。API/worker 重建后的历史成果读回不等于崩溃中间态的独立收尾恢复。后续已完成 Linux `job.finalize` 适配，并实际验证编译运行中、namespace 创建后尚未放行两处 API 退出恢复；Windows/WSL 兼容通过，见 [Linux 独立收尾恢复](linux-finalizer-recovery.md)。这些路径不代替完整故障矩阵。任意动态/绝对外部依赖冻结等 FF-E04 原范围也未缩减。

本轮为实际原生文档与基础设施测试，模型调用 0、模拟 LLM 0。主线 49/53、剩 4；资源 11/17、剩 6；全功能 2/295、剩 293。N7-05 和完整功能终验继续推进。

最终汇总：`../../tmp/mainline-linux-tex-evidence/validation.json`。实际 S3 API/worker 镜像为 `vibeflow/api:linux-tex-v6-20261118`，两容器 app 源码哈希与工作区逐文件一致。FS 部署保留原工程模板镜像。

| 验收组 | 原始证据 |
| --- | --- |
| windows-a3cddb4003 | `../../tmp/linux-tex-20261118/windows-a3cddb4003/result.json` |
| linux-4f556736de | `../../tmp/linux-tex-20261118/linux-4f556736de/result.json` |
| linux-8c3eddcd08 | `../../tmp/linux-tex-20261118/linux-8c3eddcd08/result.json` |
| linux-262f07703f | `../../tmp/linux-tex-20261118/linux-262f07703f/result.json` |
| windows-4ec6911607 | `../../tmp/linux-tex-20261118/windows-4ec6911607/result.json` |
| 源编辑/恢复、容器重建、请求停止与 Compose | `../../tmp/linux-tex-20261118/deployment-result.json` |
