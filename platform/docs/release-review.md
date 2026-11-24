# 可审查源码与运行资产包

此包用于本地交接、审查和隔离验证，包含现有功能所需静态资源，不是全功能验收完成或允许对外分发的声明。53 项主线和 295 项功能交付范围不变。目标部署、完整依赖安装及第三方分发边界仍须继续核对。

## 内容

- API 源码、全部迁移、配置模板、Linux/Windows Python 哈希锁和运维只读结构检查。
- 前端源码、npm 锁文件、完整 public 目录：包含 PDF 字符映射、字体、WASM、ONNX 及其现有许可/来源说明。
- 自有技能、工程示例、OpenFOAM 静态补全索引及其说明。
- Docker 构建/编排、启动脚本、部署/恢复和交付文档。
- `release-manifest.json`：每个文件的路径、字节数和 SHA-256；包外 `.sha256` 校验整个归档。

不携带私有 `.env`、`.vault_key`、数据库、用户算例、node_modules、Python 虚拟环境、缓存、旧模拟测试执行器或 extracted 厂商二进制。内容扫描会拒绝明显的服务密钥和私钥文本，错误仅报告路径。白名单与扫描都不是对所有可能敏感文本的绝对保证，分发前仍需审查清单。

## 创建和校验

从仓库根目录运行，输出路径必须不存在：

```powershell
platform/server/.venv/Scripts/python.exe -B platform/tools/release_package.py create platform/backups/release-review.zip
platform/server/.venv/Scripts/python.exe -B platform/tools/release_package.py verify platform/backups/release-review.zip
platform/server/.venv/Scripts/python.exe -B platform/tools/release_package.py extract platform/backups/release-review.zip --destination tmp/release-review-extracted
```

打包不执行模型、业务代码或测试，不修改用户数据库。解包只写入新目录；不会覆盖已有部署。相同源码/资产生成固定时间戳、固定顺序的 ZIP，便于比较和复核。

当前清单为 `vibeflow.internal-release-review.v2`，要求包含现行部署需要的输入文件。v1 历史清单按原始打包配方的必需文件校验，仍逐个校验包内文件、路径和包外摘要。旧归档用来还原匹配版本。

## 隔离验收

在解包后的 `platform/server` 使用独立数据库 URL、独立文件目录和测试 JWT 密钥，先执行 `python -m alembic upgrade head`，再运行 `schema_diff.py` 和实际 API 启动检查。不要复制开发 `.env` 或在解包目录连接开发库。

在解包后的 `platform/web` 用 `npm ci` 安装锁定前端依赖后 `npm run build`，确认产物包含 public 的完整资源。若验收复用既有 node_modules 或 Python 环境，记录该事实；这只能证明包内容可构建/启动，不等于全新目标环境安装已验收。

完整业务验收另用审查后的真实入口。涉及模型时必须调用已授权真实服务，不把 API 存活、迁移或前端构建当作 Agent/CLI 验收。当前环境运行及恢复步骤见 [部署说明](deployment.md)。

## 外部依赖与边界

OpenFOAM、WSL、TeX Live、Pandoc、原生 CLI、CalculiX、SU2、ParaView 及商业求解器按实际目标主机配置。包内含 CPython 3.11 Linux/Windows x86-64 的 Python 哈希锁和 npm lockfile。锁定依赖的安装证据和镜像边界见 [发布依赖](FF-J23-release-lock.md)。

现有 Texo/FormulaNet 等资产在包中保留，避免删功能来制造可构建结果。包只在本地用于审查；第三方来源及尚未解决的分发限制见 [第三方台账](third-party.md)。本轮不上传发布、不提供新的许可结论。正式 SIWC 联调按用户要求暂缓。

发布候选与源码回退包不同于数据库/算例备份。回滚需恢复匹配的代码、数据库、存储与原 provider 加密密钥；仅解压旧代码不能保证新数据库可回退。
