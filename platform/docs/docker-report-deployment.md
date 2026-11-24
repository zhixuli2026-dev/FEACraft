# Docker 原生报告导出验收

2026-11-17：API/worker 镜像已安装 Pandoc，平台正式报告接口可在 Linux 容器中生成 HTML/DOCX。已通过实际产物下载、重启后读回和真实 DeepSeek 工具读取。本项推进 N7-05 外部依赖部署范围，不代表全部专业工具链完成；主线仍 49/53，资源 11/17，295 项全功能目标保持。

## 修正的问题

原 `Dockerfile.api` 只安装 Python 和图形库，缺少 Pandoc。现在通过 Debian 软件包安装 Pandoc 和所需数据，API 与 worker 共用同一镜像，运行配置通过 PATH 发现 `/usr/bin/pandoc`。实际版本是 Pandoc 3.1.11.1；构建安装了 5 个包，没有升级或删除其他包。

第一次真实报告请求还暴露了独立代码错误：`report_handler.py` 在导入时访问文件路径的第五级父目录，容器 `/app/app/services/report_handler.py` 没有这么多层，抛出 `IndexError`，进而使注册表中的全部专业处理器无法加载。现用根目录处安全的相对路径解析保留原仓库回退路径，配置优先级不变。Windows 原 Pandoc 路径有效；Windows 与 Docker 均实际加载 30 个注册处理器。加载成功不等于逐个执行了 30 种业务。

## 实际验收

隔离项目仍为 `vf-review-20261117`，PostgreSQL/Redis、数据卷和用户原本开发环境保留。替换 API/worker 镜像前确认隔离库没有 queued/running 作业；仅重建这两个服务，并重新加载 Nginx 的 API 地址。

最终入口 `server/live_llm_docker_report_acceptance.py` 完成 5 项检查：

1. 通过实际 API 发现容器内 Pandoc。
2. 正式 `report:build` 作业成功并完成发布，根进程及 8 个所属子执行均有停止证明。
3. 下载真实 HTML 和 DOCX：随机标识及表格值在 HTML 和 DOCX 的 `word/document.xml` 中存在；工具链记录包含实际路径、版本及二进制摘要。
4. 实际重启 API/worker、重新登录后，HTML/DOCX 逐字节一致，工具链记录和作业状态保持。
5. 使用授权的官方 DeepSeek `deepseek-flash`，模型调用工具读取新生成的 HTML，返回其中随机标识；工具结果和会话结果持久保存。

真实模型应用请求 1 次，上游 HTTP 回合数和 token 未单独计量；模拟模型 0、Claude 0。报告实际作业 ID 为 7，HTML 3,028 字节，DOCX 9,961 字节。本轮没有调用 PDF/LaTeX、OpenFOAM 或其他专业工具。

## 证据与失败保留

- [最终验收结果](../../tmp/docker-deployment-20261117/reports-adfc490d96/report-result.json)、[作业及子执行证明](../../tmp/docker-deployment-20261117/reports-adfc490d96/job-terminal.json)。
- [真实模型事件](../../tmp/docker-deployment-20261117/reports-adfc490d96/live-model-events.json)、[HTML](../../tmp/docker-deployment-20261117/reports-adfc490d96/report.html)、[DOCX](../../tmp/docker-deployment-20261117/reports-adfc490d96/report.docx)。
- [658 份审查源码](../../tmp/N7-05-docker-report-reviewed-sources-v2-20261117.zip)、[审查登记](../../tmp/N7-05-docker-report-review-v2-20261117.json)。
- 首次构建遭遇 Debian 源 HTTP 502，保留 `build-api-report.log`；重试成功保留 `build-api-report-v2.log`。未关闭包签名校验。
- 导入错误导致真实 API 500，保留 `reports-a9e18618b5/report-result.json` 和该目录的 `api-failure.log`，该轮未调用模型。
- 修正后镜像构建为 `build-api-report-v3.log`；实际镜像、已安装软件包及准备请求收尾见本阶段 `deployment-receipt.json`。

这些路径位于 `tmp/docker-deployment-20261117/`；其中私有账户、实际 Compose 和密钥不进入可审查发布包。源码归档和最终发布包分别保留。

## 使用与剩余工作

按 [部署说明](deployment.md) 重建 API 镜像，并让 API/worker 使用同一版本。已有数据卷和 provider 原密钥继续保留。报告构建使用平台原有的输入封存、原生执行、发布和下载流程，无需在容器启动后手动安装 Pandoc。

Markdown 到 HTML/DOCX 已实际可用；Linux 完整 CAE、TeX、原生 CLI、目标拓扑和完整恢复矩阵继续按原计划推进。已安装的 Pandoc 不等于 LaTeX/PDF 工具链齐备，也不替代正式工作流上游工程证据门禁。
