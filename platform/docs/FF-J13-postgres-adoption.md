# FF-J13 PostgreSQL 建库与 Alembic 纳管

源码当前迁移链有 40 个版本，初始修订为 `6fb6c18fb7ee`，唯一 head 为 `228635dfe9a1`。2026-10-29 对原数据库纳管时，先保存并临时恢复 custom-format 备份，再核对结构差异为零并记录当时的 head `6fb6c18fb7ee`。只读核对旧备份目录时，`pg_restore --list` 显示 12 张业务表，备份里尚无 `alembic_version`。N7-02 在 `f9d4a10735bc` 实际验证了旧数据升级、回退、拒绝有损回退和备份恢复。之后的 J12 与 H09 分别补验输入恢复和文件观测迁移。 [原纳管记录](progress.md) · [N7-02 实际迁移](N7-02-postgres-migration-acceptance.md) · [跨版本恢复](FF-J12-completion-review.md) · [当前末版迁移](FF-H09-external-file-watch-acceptance.md)

本次在本机 PostgreSQL 中新建随机临时数据库，恢复 N7-02 的旧夹具备份及 UTF-8 日志。仅在副本中移除版本表，故意改错一个业务索引名称。以初始修订另建参照库，用 PostgreSQL 的结构导出做逐项比对。结构不符时保持无版本状态，修正后仅执行 `stamp 6fb6c18fb7ee`，再显式升级至当前 head。直接新建的当前库与纳管后升级的结构相同，两个库均通过 `alembic check`。原 12 张表的旧列内容摘要一致，当前 API 登录后能读回五条旧作业与同字节日志，三个临时库均已回收。 [实际验收入口](../server/j13_postgres_adoption_acceptance.py) · [建库与纳管回执](../../tmp/j13-postgres-adoption-8uuu0qm7/result.json)

使用当前完整结构的另一个临时副本复现了启动缺口。修改前移除 `alembic_version` 后，现有生产入口仍可启动、登录并读取五份旧日志。随后显式写入已确认的当前修订号，应用同样可用。保持两个独立进程和三库回收的原记录。 [修改前真实对照](../../tmp/j13-postgres-adoption-xgahpsg2/result.json)

修复后，完整但未登记的 PostgreSQL 副本在实际启动时被拒，API PID 34756 退出码为 3，端口 56460 已关闭。版本表依然缺失，12 张旧表的原列数据摘要一致。确认副本故障前结构与新建当前库一致后，显式登记当前修订，API PID 32772 起于端口 56485，正常登录并逐字节读回全部五份旧日志。API 已退出，端口已关闭，三个随机临时库已回收。正式验收退出码 0，真实模型调用和模拟调用均为 0。 [修复后实际回执](../../tmp/j13-postgres-adoption-gz2i1nls/result.json)

PostgreSQL 应由显式 Alembic 操作纳管。应用启动预检要求完整业务表与当前唯一修订。现有未登记数据库应先做可恢复的冷备，用对应历史修订在独立库对照结构。确认匹配后由运维显式写入匹配的修订号，再按版本升级。`schema_diff.py` 用于当前模型差异诊断，比较目标属于历史修订时应使用同版本结构。SQLite 开发库沿用原启动规则。 [数据库启动预检](../server/app/db.py) · [部署与运维](deployment.md)

本机开发 PostgreSQL 本次只读核对时已登记 `6fb6c18fb7ee`，并未迁移或重启。逆向跨版本回退需参考对应数据和备份。新增原生审计与会话关联表的降级会移除表，带这类记录的应用应采用与旧应用匹配的升级前备份恢复。既有 J12 演示的是其文档中对应版本与数据范围。 [开发库只读核对](../../tmp/ff-j13-startup-gate/development-registration-readonly.json) · [升级与回滚手册](deployment-recovery-runbook.md)
