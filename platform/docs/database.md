# 数据库结构与迁移

2026-11-19 核对，当前代码的唯一 Alembic head 是 `228635dfe9a1`。PostgreSQL 启动要求完整的业务表和已登记的当前修订。真实隔离验收覆盖历史结构对照、显式登记初始修订、升级、新建当前库和原有作业读回。本机开发库只读确认为 `6fb6c18fb7ee`。下方各日期的记录保留当时的版本证据。[当前纳管验收](FF-J13-postgres-adoption.md)

> 当前核对：2026-11-03。34张业务表，head `2c0b87d1a643`，前驱 `1b9a76c0f532`；新增 native_cli_forks 持久准入/边界/线程/归属记录。双库各147项约束、metadata diff=0、升级/回滚/再升级及有分叉记录拒绝降级通过。开发库未迁移或重载；上次只读记录仍12业务表+alembic_version、旧head `6fb6c18fb7ee`。见[原生分叉迁移](native-cli-forks.md)。

## 1. 数据分层

| 层 | 位置 | 内容 | 事实源/可重建性 |
|---|---|---|---|
| 主数据库 | PostgreSQL `vibeflow` | 用户、工作区、case 元数据、Agent、作业、组织与订阅 | 业务元数据事实源；需要备份 |
| case 文件 | `server/data/files/workspaces/{uid}/cases/{cid}/` | caseDict、几何、网格、求解、报告、日志等 | **文件事实源**；DB 的 caseDict 字段只是镜像 |
| 搜索索引 | `server/data/index/case-{id}.sqlite` | 文件名、目录元数据、FTS 内容 | 派生数据，可从 case 文件重建 |
| OpenFOAM 补全 | `server/data/completion/` | JSON 补全项与来源 metadata | 派生数据，可由源码重建，不是主 DB 表 |
| 旧 SQLite | `server/vibeflow.db` | 早期 7 表开发库 | 当前 `.env` 未使用；不要与 12 表现模型混淆 |

核心规则：**caseDict 必须先解析，再写文件，最后更新 DB 镜像并 commit**。所有 REST/Agent 写入统一经过 `update_case_dict`；普通写、上传和删除也要 canonicalize 路径，防止变体旁路。

## 2. 主库关系

```text
users ──< workspaces ──< cases ──< conversations ──< agent_events
                              │                  └─< tool_calls
                              └─< jobs

users ──< provider_profiles
users ──< memberships >── organizations ──< subscriptions >── plans
conversations ──< conversations（forked_from_id 自引用）
```

`A ──< B` 表示 A 对 B 一对多。主要外键都存在；下面“约束注意事项”列出例外。

## 3. 十二张既有主表

| 表 | 关键字段与用途 | 主要约束 |
|---|---|---|
| `users` | `id/email/name/password_hash/status/created_at` | PK `id`；`email` 唯一索引 |
| `workspaces` | `id/user_id/name/created_at` | `user_id → users.id`，索引 |
| `cases` | `workspace_id/name/s3_prefix/workflow_state/dict_revision/case_meta/parse_status/created_at/updated_at` | `workspace_id → workspaces.id`；`(workspace_id,name)` 唯一 |
| `provider_profiles` | `user_id/provider/name/base_url/default_model/key_ciphertext/created_at` | `user_id → users.id`；密钥为 Fernet 密文 |
| `conversations` | `case_id/title/forked_from_id/branch_head_event_id/provider_profile_id/model/permission_mode/created_at` | `case_id → cases.id`；`forked_from_id → conversations.id` |
| `agent_events` | `id/conversation_id/seq/event_id/type/payload/cwd/created_at` | `conversation_id → conversations.id`；`event_id` 唯一；SQLite PK 使用 Integer variant |
| `tool_calls` | 字符串 `id/conversation_id/tool/args/status/result/created_at` | 实际 ID 形如 `{cid}:{model_call_id}`；`conversation_id → conversations.id` |
| `jobs` | `case_id/type/command/status/exit_code/log_s3_key/parsed_summary/created_at/ended_at` | `case_id → cases.id`；状态含 queued/running/succeeded/failed/cancelled/interrupted |
| `organizations` | `id/name/created_at` | PK |
| `memberships` | `org_id/user_id/role` | 双外键；`(org_id,user_id)` 唯一；角色 admin/member/viewer |
| `plans` | `code/name/quotas/created_at` | `code` 唯一；配额为 JSON 文本 |
| `subscriptions` | `org_id/plan_id/status/expires_at/created_at` | 双外键；状态 active/expired |

### JSON 与时间的实际类型

- `cases.case_meta`
- `agent_events.payload`
- `tool_calls.args`
- `jobs.parsed_summary`
- `plans.quotas`

以上均是 **Text 中的 JSON 字符串**，不是 PostgreSQL JSONB。时间列使用 `DateTime(timezone=True)`；应用默认由 Python 生成时间，模型默认值不等于数据库 server default。

## 4. 约束与归属注意事项

- `conversations.provider_profile_id` **没有外键**；`branch_head_event_id` 也没有外键。应用必须防止悬空或跨用户引用。
- `agent_events` 没有 `(conversation_id,seq)` 唯一约束；并发写事件时不要假设数据库已防重。
- `subscriptions` 没有“每组织仅一个 active 订阅”的数据库约束。
- 外键未声明级联删除；删除策略不能依赖自动 cascade。
- Organization/Membership 当前主要服务套餐与配额；workspace/case 的直接所有权仍是 `workspaces.user_id`，不能默认已经实现组织协作 ACL。
- PTY/viz session 的 `user_id` 在进程内注册表中，不在主库；重启后不能按持久会话理解。

## 5. 每算例 FTS SQLite

`server/data/index/case-<id>.sqlite` 与主库分离：

```sql
CREATE TABLE files (
  path TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  parent TEXT NOT NULL,
  size INTEGER NOT NULL,
  mtime INTEGER NOT NULL,
  suffix TEXT
);

CREATE VIRTUAL TABLE file_fts USING fts5(
  path UNINDEXED,
  name,
  content,
  tokenize='unicode61'
);
```

`files` 保存目录元数据，`file_fts` 保存可搜索文本。写/删文件有增量钩子，作业结束可触发重建；外部修改 watcher 和大索引性能仍是缺口。索引损坏或缺失时可从文件事实源重建，不需要按主数据恢复。

## 6. Alembic 操作

配置：`server/alembic.ini`、`server/migrations/`。基线`6fb6c18fb7ee`为旧12表；head为`7a91c4e2d6b0`，经`0fd328dae4cf`新增工作流/步骤/attempt、尝试head、发布/发布head、事件序号、持久事件、outbox和Job提交请求共10表。此前迁移补attempt请求族/重试关联；最新迁移增加child_processes_json，双数据库各27项约束及带旧关联记录的增量往返见 [owned-native-children.md](owned-native-children.md)。

### 新数据库

```bash
cd platform/server
./.venv/Scripts/python.exe -m alembic -c alembic.ini upgrade head
```

### 已有但未纳管的数据库

先备份并只读比较：

```bash
./.venv/Scripts/python.exe schema_diff.py
```

脚本优先使用进程环境中的 `VIBEFLOW_DATABASE_URL`，否则固定读取 `server/.env`，不会退到调用目录下的临时 SQLite。输出先给出脱敏的 `DATABASE_DRIVER/HOST/DATABASE`，审查时先确认目标，再看差异；从其他工作目录调用也已验证。

- `SCHEMA_DIFF_COUNT=0`：业务表与模型一致，只缺版本标记。
- 非零：先审查差异并编写显式迁移；不要盲目 stamp，也不要依赖 `create_all` 补列。
- 未纳管时 `alembic check` 可能仅因版本不在 head 而失败。

只有数据库与**当前head的22表模型**差异为零、备份完成且审查确认纳管版本后，才可为该未纳管库标记head：

```bash
./.venv/Scripts/python.exe -m alembic -c alembic.ini stamp head
```

旧12表未纳管库不能直接stamp head；必须对照冻结的旧12表模型验证，再审查基线纳管与增量升级。当前开发库已在`6fb6c18fb7ee`，不需要重复stamp。后续升级须先备份/验证与审查：

```bash
./.venv/Scripts/python.exe -m alembic -c alembic.ini upgrade head
```

应用只对空SQLite开发库保留`create_all`便利建表，空PostgreSQL也要求显式Alembic迁移。已有业务表但缺表或Alembic版本不是代码head时，启动拒绝隐式升级，必须先完成经过审查的迁移。SQLite便利建表不会自动产生Alembic版本记录；生产须使用显式迁移流程。

### 回滚警告

`downgrade base` 会删除基线中的全部业务表。`migration_smoke.py` 只在临时 SQLite 上演练 downgrade；**禁止把临时库回滚命令直接照搬到有数据的 PostgreSQL**。生产回滚必须针对具体 revision 评审，并优先使用备份恢复方案。

## 7. 备份范围

- PostgreSQL 全量逻辑备份及恢复演练。
- `server/data/files/` 的 case 文件。
- `server/app/.vault_key`：解密 provider profile 所需；丢失后仅有密文无法恢复 LLM key。
- 私有 `.env` 应以秘密管理方式备份，不进入公开文档或普通源码包。

FTS 和 OpenFOAM 补全通常可重建，备份策略可与主数据分开。详细升级/恢复步骤见 `deployment.md`。
