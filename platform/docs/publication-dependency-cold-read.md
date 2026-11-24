# N4-05 发布依赖的冷读取

S3 上的依赖图和只做校验的发布引用，以数据库绑定的清单和实际对象为准。验证先核对文件清单，再用现有四路分块读取逐一计算文件摘要，结束后复核清单与对象路径。依赖图、历史引用和递归的上游校验可直接使用这些结果。需要把发布文件挂载给作业时，继续由原路径按预留恢复本地缓存。文件系统沿用本地摘要校验。[依赖图](../server/app/services/dependency_status.py) · [发布引用](../server/app/services/publication_inputs.py) · [S3 完整性校验](../server/app/services/publication_objects.py)

2026-11-19 用同一真实 Windows `convert` 脚本在两种部署分别发布 40 MiB 大文件和一个短文件。公开 `GET /cases/{id}/dependencies` 报告成果为 fresh。PostgreSQL／SeaweedFS 从本地发布缓存缺失状态发起请求，请求结束后仍没有新增发布缓存。S3 完整验证的 Python 分配峰值为 2,187,925 B。测量覆盖不可变包的核验，上游处理器可按自身要求另建输入视图。[文件系统回执](../../tmp/cold-dependency-fs-bxyhl5ss/cold-dependency-read.json) · [SeaweedFS 回执](../../tmp/cold-dependency-s3-j2yds7wj/cold-dependency-read.json)

已认证的夹具通过生产 `resolve` 和 `validate` 检查历史引用，然后执行实际挂载，SHA-256 相同。替换同样长度的另一文件后，公开依赖图在两个后端均标记 `bundle_unverifiable`。S3 历史引用只选中健全的大文件时，整包核验仍因损坏的短文件返回 409。另一次在真实正文摘要结束后、结果返回前替换对象，结束时的对象版本比较识别了变化。上述失败期间本地缓存保持缺失。

首次文件系统运行发现验证分支漏导入存储模块，修正后两种部署全量复验通过。隔离 API、对象存储和数据库均已核验退出或清理。真实和模拟模型调用均为 0。[首轮回执](../../tmp/cold-dependency-fs-_ls_8w6d/cold-dependency-read.json) · [临时库清理](../../tmp/terminal-agent-postgres-uhdgn4j5/cleanup.json) · [复验入口](../server/publication_dependency_cold_read_acceptance.py)
