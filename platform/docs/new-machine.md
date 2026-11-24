# 在新电脑接手 FEACraft

从 `origin/main` 克隆仓库后，在克隆目录执行以下步骤。普通 Web 开发使用 Python 3.11、Node.js 24 和临时或本地 SQLite。现有案例涉及的 OpenFOAM、ParaView、文档编译和其他专用工具按各自任务配置。

## 启动空库开发环境

在第一个 PowerShell 窗口从仓库根目录运行：

```powershell
Set-Location platform/server
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:VIBEFLOW_DATABASE_URL = 'sqlite+aiosqlite:///./vibeflow.db'
$env:VIBEFLOW_STORAGE_BACKEND = 'fs'
$env:VIBEFLOW_STORAGE_FS_ROOT = './data/files'
$env:VIBEFLOW_JWT_SECRET = ([guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N'))
.\.venv\Scripts\python.exe -m alembic -c alembic.ini upgrade head
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

在第二个窗口从仓库根目录运行：

```powershell
Set-Location platform/web
npm ci
npm run dev
```

打开 `http://localhost:5173`。健康检查地址为 `http://127.0.0.1:8000/api/health`。运行生产前端构建使用 `npm run build`。

## 核对交付计划

在仓库根目录运行 `python platform/tools/validate_roadmap.py --source-only`。这会检查主线、审计、交付项和仓库内引用，并核对随仓库提供的历史回执索引。输出中的 `indexed_evidence_not_present` 是 Git 克隆里缺少的索引回执数。恢复原始回执之后，省略 `--source-only` 即可按原方式检查本地证据链接。

## 恢复现有本地资产

从 [GitHub 换机文件](https://github.com/zhixuli2026-dev/FEACraft/releases/tag/workstation-transfer-20261125) 下载 `FEACraft-local-assets.zip` 与 `FEACraft-sha256.txt`。照摘要核对 ZIP，再把内容解压到克隆根目录。补充包包含本地公式识别资产、回执及截图、已下载的 Docker 构建输入，以及原版静态分析的输入。公式识别资产恢复后重启 Vite，发布前端时重新构建。

Node.js 和 ParaView 的 Docker 构建输入也可以用仓库内的 `platform/tools/fetch_node.py` 和 `platform/tools/fetch_paraview.py` 重新获取并核验。专业求解器、WSL 发行版和 Docker 镜像按 [开发环境](dev-setup.md) 与 [部署说明](deployment.md) 安装。

## 恢复原目录和旧数据

需要沿用这台机器的原始资料或算例时，从同一个 [GitHub Release](https://github.com/zhixuli2026-dev/FEACraft/releases/tag/workstation-transfer-20261125) 再下载 `FEACraft-original-folders-and-data.zip`。核对摘要后解压到新克隆的根目录。此包保留了旧根目录的 `data/`、完整 `extracted/`、`third_party/` 和 `tools/` 的本地资料，同时备份了实际的 `platform/server/data/files/` 和 `platform/server/data/skills/`。四个开发工具脚本从 Git 获取。可读的内部链接以目标文件内容写入，并在清单中保留链接关系。源目录里原有的失效文档链接也列在清单里。

开发数据库备份位于解包后的 `tmp/private-transfer/database/feacraft-dev-postgresql16.dump`。使用 PostgreSQL 16 的 `pg_restore --no-owner --no-privileges --exit-on-error` 还原到新机的空数据库，然后在指向该副本的配置上执行 Alembic 迁移。独立 PostgreSQL 16 已试还原并升级到源码当前版本，账号、工作区和算例记录数量与源库一致。后端从 `platform/server` 启动时，文件路径填 `./data/files`，用户技能路径填 `./data/skills`。模型凭据和部署密钥继续通过私有配置管理。接入真实模型时从受信任的本地来源重新配置 API 凭据。

Windows 版 CalculiX、SU2 和 Elmer 可以复用包内的运行目录。在启动后端的终端设置 `VIBEFLOW_CALCULIX`、`VIBEFLOW_SU2` 或 `VIBEFLOW_ELMER`，指向相应可执行文件。WSL 发行版和 Docker 数据卷由系统单独管理。
