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

本机随迁移生成了 `FEACraft-local-assets.zip` 和同名 `.sha256`。把这两个文件带到新电脑，照摘要核对 ZIP，再把内容解压到克隆根目录。补充包包含本地公式识别资产、回执及截图、已下载的 Docker 构建输入，以及原版静态分析的输入。包内含本地历史证据，仅用于私下换机。公式识别资产恢复后重启 Vite，发布前端时重新构建。

Node.js 和 ParaView 的 Docker 构建输入也可以用仓库内的 `platform/tools/fetch_node.py` 和 `platform/tools/fetch_paraview.py` 重新获取并核验。专业求解器、WSL 发行版和 Docker 镜像按 [开发环境](dev-setup.md) 与 [部署说明](deployment.md) 安装。

实际开发库、算例、用户技能和模型密钥仍随原本地存储管理。需要旧数据时，在新机恢复对应的数据库备份、文件和密钥后配置 `platform/server/.env`。接入真实模型时从受信任的本地来源重新配置 API 凭据。
