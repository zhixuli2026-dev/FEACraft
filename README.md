# FEACraft

FEACraft 是 React、FastAPI 与 OpenFOAM 构建的 CAE 工作台。历史调研和重建记录保存在 [REPORT.md](REPORT.md) 与 [交接文档](platform/docs/HANDOVER.md)。

## 开发入口

新电脑从 `origin/main` 克隆后，按 [换机接手](platform/docs/new-machine.md) 启动和核对项目。数据库与专业工具配置见 [本地开发环境](platform/docs/dev-setup.md)。

## 目录

| 路径 | 内容 | 使用方式 |
|---|---|---|
| `tools/` | 自研静态分析工具 | 查看历史分析 |
| `platform/server/` | FastAPI、Alembic、技能、示例与测试 | Web 后端 |
| `platform/web/` | React 19 + TypeScript + Vite | Web 前端 |
| `platform/infra/` | Dockerfile、compose、nginx | 候选部署产物，需目标机验证 |
| `platform/docs/` | 当前文档、进度与工作台证据 | 项目事实源 |
| `REPORT.md` | 桌面版静态逆向报告 | 了解原版架构 |

## 从这里开始

- **运行/开发平台**：先读 [dev-setup.md](platform/docs/dev-setup.md)，再读 [HANDOVER.md](platform/docs/HANDOVER.md)。
- **理解数据库**：读 [database.md](platform/docs/database.md)。
- **运行测试**：读 [testing.md](platform/docs/testing.md)。
- **部署或暴露给其他用户**：先读 [deployment.md](platform/docs/deployment.md) 和 [security.md](platform/docs/security.md)，当前单机实现不能直接视为多租户生产系统。
- **查看当前进度与未验证项**：读 [progress.md](platform/docs/progress.md)；目标方案与实现偏差见 [plan-cs-to-bs.md](platform/docs/plan-cs-to-bs.md)。
- **查看当前实施计划**：读 [下一大阶段主线](platform/docs/next-stage-mainline-plan.md)（53 项）和 [全功能交付计划](platform/docs/full-function-delivery-plan.md)（295 项）；执行状态、证据和剩余工作同步维护在文档关联台账中。早期工作包映射保留在 [implementation-master-plan.md](platform/docs/implementation-master-plan.md)。
- **第三方资产与 IP 边界**：读 [third-party.md](platform/docs/third-party.md)。

## 当前技术形态

React 19 + dockview + Monaco + Three.js/xterm；FastAPI + SQLAlchemy async + Alembic；PostgreSQL 元数据与本地文件事实源；WSL OpenFOAM；CadQuery 几何；VTK/trame 可视化；OpenAI/Anthropic 双协议 Agent。

当前验证结果、历史记录和未执行项不在这个入口长期固化，统一以 [progress.md](platform/docs/progress.md) 为准。

## 边界与注意事项

- 原始安装包、解包目录、第三方工具安装与旧测试目录保存在 `D:\vibeflow_reverse`。文档引用的历史验收文件在本机 `tmp/` 按相对路径保存，Git 忽略该目录。完整证据核验需要本地回执。
- Texo 公式识别运行资产已保留供本机开发，Git 仅纳入其许可证和来源说明。 [第三方资产记录](platform/docs/third-party.md)
- 本机的 `platform/server/.env` 沿用既有 PostgreSQL、算例存储、用户技能和模型凭据库。这些数据及其密钥保存在原存储位置。删除原目录前需要另行迁移这些本地路径。
- 本次在临时库验证了 Alembic 完整迁移。既有 PostgreSQL 登记为 `6fb6c18fb7ee`，距当前源码版本 `c05f58d0c4e2` 缺少 53 张表。采用这份既有数据启动新版前需要单独处理数据库迁移。
- 模型相关验收依照 [AGENTS.md](AGENTS.md) 调用已授权的真实服务。
- Docker/compose 候选产物仍需目标机端到端验证；宿主命令执行和单进程状态模型的限制见 [security.md](platform/docs/security.md)。
