# FF-J22 CI/CD 与每日回放

2026-11-25，根据用户决定，FF-J22 已移出本次交付。下文为历史记录。 [当前交付范围](full-function-delivery-plan.md#21-最新范围规则)

2026-11-25，本机 31 个已审查前端套件、9 个后端离线套件、隔离 API 和跨版发布回滚通过。全功能台账 269/295，剩 26 项，主线 51/53。FF-J22 继续实施。

## 回归入口

`.github/workflows/platform-ci.yml` 将后端离线、隔离 API 和前端拆成独立作业。原选择范围保留。每个作业在执行套件前使用 `run_regression.py` 核对审查清单及源码 SHA-256，后端待审查套件会阻止整个后端选择启动。需要一并核查 240 个入口时可调用 `--ci-stage preflight`，该参数只检查清单。

| 本机验收 | 结果 | 回执 |
|---|---|---|
| 31 个前端离线套件 | 31 个通过，真模型与模拟模型各 0 次 | [全部执行日志](../../tmp/ff-j22-ci-policy-20261124/frontend-all-verified.log) |
| 工作区生产构建 | TypeScript 与 Vite 均通过，保留现有分包体积警告 | [构建日志](../../tmp/ff-j22-frontend-build-20261124/vite.log) |
| 单独的 API 完整性 | 实际隔离 API 完成核心业务检查，真模型与模拟模型各 0 次 | [API 日志](../../tmp/ff-j22-ci-policy-20261124/integrity-reviewed.txt) |
| 后端离线定向 | 动画轨道、色图、求解日志 3/3 通过，真模型与模拟模型各 0 次 | [定向回执](../../tmp/ff-j22-ci-policy-20261124/backend-pure-20261124/summary.json) |
| 后端配置和台账 | 实际 Compose 配置渲染与审计校验 2/2 通过，真模型与模拟模型各 0 次 | [配置回执](../../tmp/ff-j22-ci-policy-20261124/backend-config-20261124/summary.json) |
| 后端字典与依赖图 | 字典类型严格校验、旧输入、产物摘要及来源追溯，2/2 通过，模型调用 0 次 | [执行回执](../../tmp/ff-j22-ci-policy-20261125/dictionary-artifact-graph/summary.json) |
| 后端 OpenFOAM 控制文件和重算计划 | 保留字典内容 10 项、合法复用边界 15 项、拒绝无效计划 10 项，2/2 通过，模型调用 0 次 | [执行回执](../../tmp/ff-j22-ci-policy-20261125/control-workflow-plan/summary.json) |
| 后端离线全集 | 208 个入口中 9 个已审查，199 个仍有审查问题，全集在运行任何套件前停止 | [审查盘点](../../tmp/ff-j22-ci-policy-20261125/coverage.json) |

前端复验覆盖重复录音结束、前一次录音迟到事件、文件编辑冲突保留草稿、PDF/文档已选发布版本失败时的提示、LaTeX 算例路径、货币与行内公式边界。Native Chat 的离线套件只检查事件时序、完整性与重复事件；实际回复见专项模型回执。[真实 Codex 与 DeepSeek](FF-F56-windows-sandbox.md)

## 每日真实模型回放

`platform-live-replay.yml` 提供每天 04:17（北京时间）及手动触发，使用受保护的 `platform-live-replay` 环境。启用变量为 `VIBEFLOW_ENABLE_LIVE_REPLAY=true`。使用授权人员配置到该环境的 `VIBEFLOW_REPLAY_DEEPSEEK_KEY`。回放只向 `https://api.deepseek.com/v1` 发送请求，使用 `deepseek-flash`。CI 上传的回执保留结果、实际服务状态和用量，排除回复、输入和密钥。普通 PR 回归继续保持离线。[每日工作流](../../.github/workflows/platform-live-replay.yml)

本机分别以已配置的授权资料和每日入口验收，分别 4 次官方推理 HTTP 200，合计 8 次，另各有 1 次查询模型目录。两轮均覆盖翻译、流式回复、读取随机值和持久化，模拟请求 0。每日入口完成 9,816 个有回执的流式 token，另 1 次翻译未记录完整用量。缺少授权密钥时启动失败，没有创建回执或调用模型。[授权资料回放](../../tmp/live-deepseek-wwxstnl9/live-result.json) · [每日入口脱敏回执](../../tmp/ff-j22-ci-policy-20261124/live-ci-receipt.json)

## 发布与回滚演练

当前打包配方补齐两份 Python 哈希锁，API 镜像构建从源码包读取 Linux 锁。v2 候选包含 2,119 个校验文件，两份锁与源码逐字节核对。单独核验移除 Linux 锁后的 v2 归档会被拒绝。历史 v1 包按原版必需文件及归档清单验收，11 月 16 日的 1,333 个文件和 N7-05 的 1,439 个文件都通过。 [打包工具](../tools/release_package.py) · [归档口径](release-review.md) · [候选 SHA-256](../../tmp/ff-j22-release-20261125/candidate-v2.zip.sha256)

从 N7-05 归档和当前 v2 候选分别解包，复用本机 Python 3.11 环境，在独立的固定摘要 PostgreSQL 16 容器及私有文件目录运行两版 API。实际数据库从 `d31f98a254ca` 迁移到 `f71a46d08e9b`，候选版可读取先前算例并写入新文件及工作区。将升级前的 `pg_dump`、算例目录和同一私有密钥文件恢复到独立副本。旧版可登录并读回原算例，升级后才创建的记录没有进入回滚副本。重启候选 API 后仍可从升级库读回新增文件和工作区。四次 API 进程均通过退出核对，隔离 PostgreSQL 已回收，模型调用 0 次。 [演练入口](../server/release_rehearsal.py) · [实测回执](../../tmp/ff-j22-release-20261125/rollback.json)

首轮演练少了新部署必需的私有 vault key，旧 API 按预期拒绝启动。复跑调用现有初始化工具为隔离部署生成新 key，快照时核对同一文件的摘要。全程原 11 个运行容器的完整 ID 相同。阶段前后 C 盘可用为 40,721,948,672 与 40,711,020,544 字节，Docker 构建缓存保持 5.819 GB，回收 0 B。 [首次回执](../../tmp/ff-j22-release-20261125/rollback-first.json) · [C/D 盘](../../tmp/ff-j22-release-20261125/disk-after.json) · [Docker 占用](../../tmp/ff-j22-release-20261125/docker-after.txt)

GitHub Actions 配方按 PR 基准提交与候选版本独立打包，支持人工指定上一版本，使用锁定 Linux 依赖与固定 PostgreSQL 摘要。发布阶段只上传回滚结果 JSON。本机解析核对了 workflow，实际 GitHub 运行回执继续待验收。 [回滚工作流](../../.github/workflows/platform-release-rehearsal.yml)

## 剩余

- 逐套审查后端全集及其测试辅助代码。能用协议或故障记录验证的保留在离线集合。涉及推理的用获授权的真实模型入口完成验收。
- 仍需远端 CI 执行证据和完整服务/WSL/浏览器矩阵。真实模型每日入口与发布回滚已在本机跑通，两个工作流须在已授权项目中完成远端执行。当前目录无 Git 远端或 CI 运行回执。
