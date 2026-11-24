# N4-05 D 阶段的求解成果核验

D 阶段核验直接使用已解析的求解发布清单。S3 不可变成果经分块完整核验，实际求解证据另按清单摘要校验。实时输出列表使用对象元数据，并在比较前后核对版本。文件系统和 S3 共用数值时刻规则，保留输出路径变化和同值时刻目录的拒绝行为。兼容路径归档查询也直接核验 S3 对象，正式挂载继续使用现有缓存和预留。[求解门禁](../server/app/services/solve_publication.py) · [时刻规则](../server/app/services/solve_artifacts.py) · [兼容路径查询](../server/app/services/artifact_publication.py)

用导入的导热算例实际运行 CadQuery、OpenFOAM 网格与 `laplacianFoam`，再进行 B、C、D 确认。最终实现的文件系统复验证实，改动场文件、加入额外输出时刻和加入同一数值的第二个目录均返回 409，原修订保持。恢复后通过 D 阶段并核验 API 退出。[文件系统回执](../../tmp/solve-d-inventory-fs-_6wkqqah/solve-d-inventory.json)

首轮 PostgreSQL／SeaweedFS 试验遇到本地 HTTP 请求超时。增加隔离夹具的请求和作业等待时间后，第二轮通过三个拒绝和正常确认，却发现文件事务后的兼容路径登记重建了求解缓存。因此后续改用直接清单核验并复跑相同场景。两轮旧回执保留。[请求超时轮次](../../tmp/solve-d-inventory-s3-7z6vn8b7/solve-d-inventory.json) · [缓存重建轮次](../../tmp/solve-d-inventory-s3-e6c5bhtz/solve-d-inventory.json) · [复验入口](../server/solve_gate_inventory_acceptance.py)

修复后的 PostgreSQL／SeaweedFS 复验通过。三个原生作业提交了几何、网格和求解成果。三种改动均返回 409，恢复原始输出后，D 阶段确认返回 200，工作流进入 `succeeded`。实时求解目录中与确认无关的 40 MiB 对象正文 GET 为 0，确认提交后本地求解缓存仍缺失。实际 API 和对象存储已退出，隔离数据库已删除。两种存储共验收六个原生作业，真实模型及模拟调用均为 0。[PostgreSQL／SeaweedFS 回执](../../tmp/solve-d-inventory-s3-cibpx558/solve-d-inventory.json) · [数据库清理回执](../../tmp/terminal-agent-postgres-13bhszq5/cleanup.json)
