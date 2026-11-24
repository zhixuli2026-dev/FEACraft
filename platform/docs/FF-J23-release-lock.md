# FF-J23 发布依赖、镜像清单和漏洞复核

发布配方锁定 Linux 和 Windows x86-64 的 CPython 3.11 依赖。两份锁分别包含 103 个包，来自已通过 `pip check` 的实际运行环境。API 以哈希校验安装二进制 wheel，系统库使用带签名的 Debian trixie 快照。API、Web、Ubuntu 沙箱基础镜像及生产 Compose 的 PostgreSQL、Redis、SeaweedFS 均引用已核对的镜像摘要。 [Linux 锁](../server/requirements-linux-amd64-cp311.lock) · [Windows 锁](../server/requirements-windows-amd64-cp311.lock) · [镜像配方](../infra/Dockerfile.api) · [生产 Compose](../infra/docker-compose.prod.yml) · [PyPI 分发完整性](https://docs.pypi.org/api/json/)

Windows 的 103 个 wheel 也已逐个核验 SHA-256。在 D 盘新建 CPython 3.11 环境，使用 `--no-index --require-hashes --only-binary` 安装，`pip check` 通过。 [Windows wheel 回执](../../tmp/FF-J23-release-lock-20261124/windows-wheel-receipt.json) · [离线安装记录](../../tmp/FF-J23-release-lock-20261124/windows-clean-install.log)

## 实际候选构建

- 首次 API 构建中 `botocore` 下载中断，哈希不符后安装停止。随后在 D 盘逐个完整下载、验证 103 个对应 Linux wheel，再以同一锁文件离线安装。基于精简 Linux 镜像的无外网健康检查还发现 Windows 专用 Elmer 路径在导入时异常，移至 Windows 分支后，候选 API `/api/health` 返回 200。 [wheel 回执](../../tmp/FF-J23-release-lock-20261124/linux-wheel-receipt.json) · [Linux 构建](../../tmp/FF-J23-release-lock-20261124/api-runtime-fix-build.log) · [健康检查](../../tmp/FF-J23-release-lock-20261124/api-runtime-fix-health.log)
- Docker daemon 拉取基础镜像遇到 503。通过主机已配置代理从 Docker Registry 获取同一 manifest，核验索引、linux/amd64 manifest、config 和 8 层 SHA-256，并核对 Docker 导入后的配置 ID。候选 Web 以精确匹配的本地基础镜像构建，`npm ci` 使用项目 lock，zlib `1.4-r0` 使用 Alpine 签名包。实际 Nginx 配置及首页和前端路由返回验证通过。 [镜像导入](../../tmp/FF-J23-release-lock-20261124/nginx-import-proof.json) · [Web 构建](../../tmp/FF-J23-release-lock-20261124/web-offline-build.log) · [Web 健康检查](../../tmp/FF-J23-release-lock-20261124/web-candidate-health.log) · [Alpine 修复记录](https://security.alpinelinux.org/vuln/CVE-2026-4738)

## 清单和发布条件

- Syft 1.57.2 为已运行的 API、Web 镜像及候选容器生成 CycloneDX。候选清单在对应镜像的隔离容器内扫描实际文件系统，元数据带镜像 ID。npm 运行依赖另有 75 个组件的清单，补充静态打包应用的依赖。 [候选 API](../../tmp/FF-J23-release-lock-20261124/sbom-out/candidate-api-fixed.cdx.json) · [候选 Web](../../tmp/FF-J23-release-lock-20261124/sbom-out/candidate-web-fixed.cdx.json) · [npm 清单](../../tmp/FF-J23-release-lock-20261124/web.cdx.json) · [Docker SBOM 说明](https://docs.docker.com/build/metadata/attestations/sbom/)
- Grype 0.122.1 使用 2026-11-23 漏洞库。采用固定 `zlib` 版本的 Web 候选复扫中，该严重匹配已消失，仍有 10 个 High 和 7 个 Medium 匹配。Linux API 候选文件系统扫描仍有 1 个 Critical、100 个 High，其余结果保留原始记录。发行版公告、无修复和 Python 打包工具匹配仍需逐项处理。两份候选与现有镜像的目录扫描范围不同，统计不直接换算为风险降幅。 [数据库记录](../../tmp/FF-J23-release-lock-20261124/grype-db.json) · [API 复扫](../../tmp/FF-J23-release-lock-20261124/candidate-api-fixed-vulnerabilities.json) · [Web 复扫](../../tmp/FF-J23-release-lock-20261124/candidate-web-fixed-vulnerabilities.json) · [扫描格式说明](https://oss.anchore.com/docs/guides/vulnerability/scan-targets/)

## 2026-11-24 发布候选和独立重建

最终 API 和 Web 候选以 `release-distribution-20261124` 分别标记，清单绑定实际镜像 ID。API 增补了签名快照的 Mesa EGL，使无显示设备的容器可渲染 VTK。实际 HTTPS、终端和 VTK 共同验收通过，另测 CadQuery 实体体积及 Pandoc 渲染。 [发布收据](../../tmp/FF-J23-release-lock-20261124/release-distribution.json) · [最终运行验收](FF-J18-tls-connection-tickets.md)

独立离线重装的 103 个 Linux Python 包中，14,155 个带哈希的已安装文件与候选逐字节一致。使用同一 Node 摘要重新执行 `npm ci` 和 Vite 构建，379 个应用文件逐字节一致。第 380 个静态文件是继承自已验证 Nginx 基础镜像的 `50x.html`，独立核对的哈希一致。比对记录覆盖应用制品和依赖文件，Docker 20.10 的镜像构建时间元数据仍可能改变 OCI 摘要。 [Python 重建](../../tmp/FF-J23-release-lock-20261124/repro-linux-python.json) · [候选静态文件](../../tmp/FF-J23-release-lock-20261124/repro-web-candidate.sha256) · [独立静态文件](../../tmp/FF-J23-release-lock-20261124/repro-web-independent.sha256) · [空间记录](../../tmp/FF-J23-release-lock-20261124/repro-after.json)

从 API 运行层移除未被依赖的构建工具 `setuptools` 后，其内置 wheel 和 jaraco.context 两个 High 匹配消失，`pip check` 和实际渲染通过。最终 API 有 1 个 Critical 和 98 个 High 匹配，Web 有 10 个 High 匹配。Grype 数据库将 API 的 66 个记录标为 `wont-fix`、33 个标为 `not-fixed`，Web 的 10 个 OpenSSL 记录为 `unknown`。这些匹配保留在逐项处置清单中。Critical `CVE-2026-6653` 来自 Mesa/LLVM 的 Debian 系统库依赖 `libxml2`，Debian trixie 标注 `no-dsa`，本次仍保留为未修复。Linux 图形和 TLS 运行依赖待发行版更新后重建复扫。 [风险与匹配](../../tmp/FF-J23-release-lock-20261124/release-risk-disposition.json) · [API SBOM](../../tmp/FF-J23-release-lock-20261124/sbom-out/distribution-api.cdx.json) · [Web SBOM](../../tmp/FF-J23-release-lock-20261124/sbom-out/distribution-web.cdx.json) · [Debian CVE 记录](https://security-tracker.debian.org/tracker/CVE-2026-6653)
