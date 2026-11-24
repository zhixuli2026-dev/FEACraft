# FF-B23 caseDict 几何参数注入

2026-11-19，在隔离 SQLite 与文件系统上通过五个实际 Windows CadQuery 作业。算例脚本从 `ctx.params` 读取三个基础尺寸，从 `ctx.domain` 读取分轴缩放和平移，从完整 `ctx.data` 读取算例名称与几何块外的参考高度。自动预览、手动预览与显式导出均消费封存的 caseDict。

| 场景 | 原生结果 |
| --- | --- |
| 全文条件保存、自动预览与首次导出 | 实体三轴范围为 `(-2, -8, 4)` 至 `(10, 2, 6)`，体积 240。 |
| 通过几何块端点只修改 `domain`、随后手动预览 | 实体三轴范围变为 `(-14, 6.5, -1)` 至 `(2, 11.5, 5)`，体积 480。 |
| 将比例设为零、手动预览 | 脚本在创建实体前因比例无效而失败，发布记录标记失败。旧场景仍可读取，阶段 B 推进被拒绝。 |
| 恢复原有效比例、显式重新导出 | 原生结果保持更新后的三轴范围与体积，发布摘要匹配，确认后进入阶段 B。 |

全文与块写入均带预期版本和原文 SHA-256。块写入前后，几何块外文本逐字节相同，原 `parameters` 保留。旧版本提交返回 409，当前内容保持。两代 STEP 均通过发布接口实际下载并由独立 CadQuery 读回，得到各自的三轴坐标、体积以及单实体六面拓扑。通过原发布 ID 多次读取的首代 STEP 字节一致。其他账户读取 caseDict、修改几何块与读旧 STEP 均返回 404。

五个作业均记录了已核验的停止回执。隔离 API 原进程已独立确认退出，真实模型与模拟模型均调用 0 次。本轮增加正式流程验收，沿用原几何执行器。[验收回执](../../tmp/geometry-parameter-injection-080gvnmk/geometry-parameter-injection.json) · [独立退出核验](../../tmp/geometry-parameter-injection-080gvnmk/cleanup-independent.json) · [复验入口](../server/geometry_parameter_injection_acceptance.py) · [触发前审查](../../tmp/FF-B23-geometry-parameters-20261119/source-review.json)
