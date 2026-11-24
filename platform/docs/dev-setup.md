# 本地开发环境

## 当前形态

- React 19 + TypeScript + Vite（`web/`）
- FastAPI + SQLAlchemy async（`server/`）
- 本机 PostgreSQL 16，数据库 `vibeflow`
- 本地文件存储 `server/data/files/`（`VIBEFLOW_STORAGE_BACKEND=fs`）
- WSL `vibeflow-linux` + OpenFOAM，用于真实求解、LaTeX 等任务
- 模型相关验证仅使用已授权的真实服务；不得启动 mock LLM 或用固定回复替代

Docker/SeaweedFS 是可选部署形态，不是当前本机开发的前置条件。

## 依赖

- Node.js 24+
- Python 3.11+
- PostgreSQL 16（当前使用 Windows 既有服务）
- 可选：WSL Ubuntu + OpenFOAM + TeX Live

首次创建虚拟环境并安装 Python 依赖：

```bash
cd platform/server
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt
```

安装前端锁定依赖：

```bash
cd ../web
npm ci
```

`requirements.txt` 包含 FastAPI、Alembic、数据库驱动、VTK/trame、CadQuery、aiohttp、matplotlib，以及 Windows 下的 pywinpty。pandoc、OpenFOAM、latexmk 等外部程序不由 pip 安装。

PostgreSQL 可使用既有实例，也可新建空库和专用账号（由管理员在 psql 中执行）：

```sql
CREATE ROLE vibeflow_app LOGIN PASSWORD 'replace-with-a-private-password';
CREATE DATABASE vibeflow OWNER vibeflow_app;
```

已有 `vibeflow` 数据库时不要重复创建；也不要在文档或命令历史中留下真实密码。

## 配置

`server/.env` 使用 `VIBEFLOW_` 前缀。先复制模板再填入私有值：

```bash
cd ../server
cp .env.example .env
```

当前开发形态的关键配置为：

```dotenv
VIBEFLOW_DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@127.0.0.1:5432/vibeflow
VIBEFLOW_STORAGE_BACKEND=fs
VIBEFLOW_STORAGE_FS_ROOT=./data/files
VIBEFLOW_JWT_SECRET=replace-with-a-random-secret
```

不要把真实密码、JWT 密钥或 LLM API key 写入文档或提交到版本库。

## 可选 WSL 求解环境

基础 API/前端开发不要求 WSL；真实 OpenFOAM、WSL LaTeX 和 WSL 终端需要既有 `vibeflow-linux` 发行版。先检查：

```bash
wsl -d vibeflow-linux --exec bash -lc 'source /usr/lib/openfoam/openfoam2406/etc/bashrc && command -v blockMesh && command -v icoFoam && command -v wmake && command -v latexmk'
```

当前求解/UDF 测试以官方 v2406 开发环境为准；系统默认仍可能指向 1912，不能只执行裸 `icoFoam`。`--exec` 避免 WSL 默认 shell 在 source 前展开命令中的变量。缺件时先完成 Ubuntu/OpenFOAM/TeX Live 安装，再用应用的 `runtime/capabilities` 自检。历史安装记录和网络绕行见 `progress.md`；不要把 Windows 的 pywinpty 当成 Linux 容器终端实现。

注册 LaTeX 编译还需要 WSL 中的 `python3`、`strace`、`latexmk`、所选 `xelatex/pdflatex/lualatex`、`xdvipdfmx`、`biber`、`bibtex`、`kpsewhich` 和 `synctex`。准备阶段实际探测这些工具并记录哈希；缺件时不会创建伪成功 Job。宿主端使用 requirements 中的 `pypdf` 校验 PDF 内容。封存资源、系统依赖及安全边界见 [latex-workflows.md](latex-workflows.md)。

## 数据库迁移

### 新数据库

```bash
cd platform/server
./.venv/Scripts/python.exe -m alembic -c alembic.ini upgrade head
```

基线迁移会创建 12 张业务表。`migration_smoke.py` 只在临时 SQLite 上验证 upgrade/downgrade，不接触开发 PostgreSQL。

### 已有但未纳管的数据库

先做只读结构对比：

```bash
./.venv/Scripts/python.exe schema_diff.py
```

- `SCHEMA_DIFF_COUNT=0`：业务表结构与模型一致，只缺 Alembic 版本标记。
- 有差异：先审查并编写显式迁移，不要直接 stamp，也不要依赖 `create_all` 加列。
- 未纳管数据库上，`alembic check` 可能因为版本不在 head 而失败；这不等于业务表有结构漂移。

只有确认差异为零、完成备份并明确决定纳管后，才执行基线标记：

```bash
./.venv/Scripts/python.exe -m alembic -c alembic.ini stamp 6fb6c18fb7ee
```

截至 2026-10-24，本机 PostgreSQL 的只读对比结果为零差异，**尚未执行 stamp**。

注意：应用启动时仍保留历史 `create_all` 兼容逻辑；它只能创建缺失表，不能替代 Alembic 的列或约束迁移。

## 启动

在 `D:\FEACraft` 启动前确认 `server/.env` 指向已完成迁移的开发库。现有库的版本差异见 [仓库说明](../../README.md#边界与注意事项)。

一键启动：

```powershell
powershell -File .\platform\scripts\dev-up.ps1
```

2026-10-24 已在用户明确授权的一次进程级策略豁免下实际启动通过；是否需要 `-ExecutionPolicy Bypass` 取决于本机策略，使用该旗标前应自行评估，脚本不会修改系统策略。脚本二次启动时的 PID 清理路径未单独复验。后端健康检查使用 `/api/health`。

脚本只清理自身 PID 文件记录、且可执行路径与命令行均匹配的后端及历史 mock 残留。2026-11-16 已删除模拟模型的启动和健康检查，现在启动：

| 服务 | 地址 |
|---|---|
| FastAPI | http://127.0.0.1:8000 |
| Vite | http://localhost:5173 |

脚本读取 `VIBEFLOW_STORAGE_BACKEND`：当前 `fs` 模式跳过 Docker/SeaweedFS；只有 `s3` 模式才启动相应存储基础设施。

也可手动分别启动：

```bash
cd platform/server
./.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

cd platform/web
npm run dev
```

## 验证

后端回归先按 AGENTS.md 审查入口及导入的辅助代码，通过 `platform/tools/regression-llm-review.json` 登记后执行。模型回复、流式输出、工具选择和 Agent 回合必须走真实模型入口；历史测试清单不作为可直接执行的命令。

```bash
cd platform/web
npx tsc --noEmit
npm run build
npm run lint
```

历史套件和浏览器记录见 `docs/HANDOVER.md`，不得未经审查直接重跑。测试状态以当次实际输出为准。当前 worker 与部署入口见 [deployment.md](deployment.md)。

## 端口

| 端口 | 用途 |
|---|---|
| 5173 | Vite 前端 |
| 8000 | FastAPI 后端 |
| 5432 | 本机 PostgreSQL |
| 9223 | 浏览器 E2E 调试 |
| 8333 / 8888 / 9333 | 仅 S3 模式的 SeaweedFS |

当前 Docker 产物仍需在可用目标机验证；Windows/pywinpty 终端路径也不能直接视为 Linux 容器终端已实现。
