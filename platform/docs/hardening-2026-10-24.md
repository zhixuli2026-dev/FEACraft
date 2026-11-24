# 平台加固与数据一致性实施计划（2026-10-24）

## Problem Statement

把当前单服务器开发实现中已确认的越权、caseDict 镜像旁路和数据库不可迁移问题修掉，同时保持现有功能不回归；实库变更必须先有差异证据，不自动改写 PostgreSQL。

## Options Considered

1. **在内存会话上记录 owner，并在每个使用点校验**：改动最小，符合当前单进程会话注册表；采用。
2. 每次使用会话都回查数据库：可覆盖旧进程数据，但把同步 WS 路径和 DB 生命周期耦合；当前不采用。
3. Alembic 只建立与当前模型一致的基线，并输出实库差异： fresh 数据库可重复建库，已有数据库不自动 stamp/upgrade；采用。
4. caseDict 由一个领域写函数负责 parse → 写文件 → 更新 Case 镜像，REST 与 Agent 共用；采用。

## Prerequisites

- [x] 已阅读相关路由、服务、模型和现有 smoke。
- [ ] 后端与 mock LLM 可用；测试只使用测试账号和 mock provider。
- [ ] PostgreSQL 只读 introspection 成功后再判断实库动作。

## Steps

### Step 1: 终端会话归属

**Action**: 先新增越权 smoke 并确认旧实现失败；再给 PTY session 记录 user_id，WS 挂接、REST 列表和删除仅允许所有者。
**Files**: `server/terminal_smoke.py` 或 `server/security_smoke.py`、`server/app/routers/terminal.py`
**Success**: 未认证 REST 为 401/403；其他用户不能看见、挂接或关闭会话；所有者和重连行为不变。
**Time**: 30 min

### Step 2: Provider profile 归属

**Action**: 运行同一 security smoke 直到命中 profile 越权；统一 owned-profile 查询，保护 conversation 创建、fork、compress、approve 和 run loop。
**Files**: `server/security_smoke.py`、`server/app/routers/agent.py`
**Success**: 新引用和历史异常引用均不能调用他人 profile；approve 在写文件/执行工具前就拒绝。
**Time**: 30 min

### Step 3: Viz proxy 归属

**Action**: 在 viz smoke 增加第二用户 proxy 断言并先复现；viz session 记录 user_id，HTTP/WS proxy 共同校验。
**Files**: `server/viz_smoke.py`、`server/app/routers/viz.py`、`server/app/routers/vizproxy.py`
**Success**: 其他用户 proxy 为 404/WS 拒绝；所有者 HTTP、wslink 和关闭流程不回归。
**Time**: 30 min

### Step 4: caseDict 统一写入

**Action**: 新增同步 smoke；提取统一写函数，REST 整写/块写及 Agent 两种文件提案均经过 parse → 文件 → DB 镜像。
**Files**: `server/casedict_sync_smoke.py`、`server/fork_compress_smoke.py`、`server/app/services/casedict.py`、`server/app/services/tools.py`、`server/app/routers/files.py`、`server/app/routers/agent.py`
**Success**: workflow_state/case_meta/dict_revision 与文件一致；非法 Agent 写入不落盘且不增加 revision；普通文件提案行为不变。
**Time**: 30 min

### Step 5: Alembic 基线与实库差异

**Action**: 先用临时数据库写 migration smoke；再加入 Alembic 配置和当前模型基线，验证 fresh upgrade/downgrade；只读比较 PostgreSQL。
**Files**: `server/migration_smoke.py`、`server/alembic.ini`、`server/migrations/`、`server/requirements.txt`、`docs/dev-setup.md`
**Success**: 空库 upgrade 得到 12 表；重复 upgrade 无变化；实库差异有报告，未经确认不 stamp/upgrade。
**Time**: 30 min

### Step 6: 启动、依赖和文档清理

**Action**: fs 模式不再启动/检查 SeaweedFS；补齐直接运行依赖；同步 React 版本、当前启动方式、API 与迁移说明。
**Files**: `scripts/dev-up.ps1`、`server/requirements.txt`、`infra/Dockerfile.api`、`docs/README.md`、`docs/HANDOVER.md`、`docs/dev-setup.md`、`docs/api.md`、`docs/progress.md`
**Success**: 文档不再把规划当现状；启动动作与所选后端一致；干净安装所需直接依赖有声明。
**Time**: 30 min

### Step 7: 全量验证与记录

**Action**: 跑新增测试、相关 smoke、前端 typecheck/build/lint；随后跑可承受的全量 smoke，记录未执行或环境阻塞项。
**Files**: 本计划、`docs/progress.md`
**Success**: 所有已执行检查有真实输出；没有把所有测试全绿建立在历史记录上。
**Time**: 30 min

## Rollback

- 代码无 Git 仓库，不执行 commit；回滚按本次修改文件逐项还原。
- 测试创建的会话在 finally 中关闭；测试文件删除；不写真实 LLM。
- Alembic 只操作临时数据库。现有 PostgreSQL 的 stamp/upgrade 需另行明确确认，并先备份和审查差异。

## Status

Started: 2026-10-24
Current Step: 7（完成）
Status: Complete

### Notes

- 两个初始 RED 已确认：caseDict 缺统一写入接口；Alembic 未接入。
- 匿名终端列表的实际请求从 HTTP 200 修为 401；完整双用户安全回归随后通过。
- 临时 SQLite 迁移和实库只读对比均通过，实库 `SCHEMA_DIFF_COUNT=0`，未 stamp。
- 前端正式 build 初次暴露 4 个类型错误，已最小修复并构建通过；lint 有既有警告。
- 主浏览器 E2E 已激活终端并验证回显、Agent、文件/PDF；截图 `shots/hardening-web-shell.png` 已查看。

### Completed

- [x] Step 1：terminal_smoke 与 security_smoke 均通过
- [x] Step 2：security_smoke 全部通过；有效 profile 的 agent_tools_smoke 双协议通过
- [x] Step 3：viz_smoke 通过，跨用户 proxy 404、所有者 proxy 200
- [x] Step 4：单元、REST 镜像和普通写入/上传/删除路径变体测试全部通过
- [x] Step 5：临时 SQLite 迁移通过；实库零差异，未 stamp
- [x] Step 6：代码/文档已整理，dev-up 语法及一次实际启动通过；二次 PID 清理未单独复验
- [x] Step 7：19/19 后端/专项套件、前端 build、主浏览器 E2E、pip check、实库差异与健康检查均有实际输出；警告和阻塞已记录

### Blockers

- 本计划范围内无阻塞。dev-up 已在用户授权的一次进程级策略豁免下实际启动通过，后端 `/api/health` 200；二次 PID 清理未单独复验。Docker 引擎和第三方许可证属于后续独立任务。
