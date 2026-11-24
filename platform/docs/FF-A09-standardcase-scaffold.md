# FF-A09 StandardCase 占位目录与脚本

平台新建算例时生成标准目录和占位文件。正式文件接口读到初始 A 阶段的 `caseDict`、空 `memory.md`、可解析的几何入口及后处理脚本。网格、求解与参考目录通过空占位文件保留。报告、UDF 和摘要各有初始文件。四种部署的新建、权限、重名、重启与创建事件证据见 [StandardCase 创建验收](FF-A01-completion-review.md)。模板定义见 [casedict.py](../server/app/services/casedict.py)。

## 2026-11-19 真实 ZIP 导入

从正式 API 读取新建算例的 12 个占位文件，对 Python 脚本进行语法解析，再导入带单层外部目录、真实 caseDict、自定义几何脚本和 40 MiB 普通文件的 ZIP。实际导入保留原作者脚本及空网格占位，原算例的 12 个文件逐字节保持。正式文件下载对大文件复核长度与 SHA-256。

ZIP 解压逐块生成普通文件的大小和摘要。提交按原创建流程预留资源，复制时重新核对已准备记录。若暂存文件在发布前被改动，实际请求收到 409，未新增可见算例。使用相同名称重新提交有效 ZIP 后成功。

SQLite 与文件系统通过最终源码的完整场景。40 MiB 导入整次请求的 Python 分配峰值为 17,055,709 B，结束后节点临时预留为 0，实际 API 已退出。模型调用和模拟调用均为 0。[文件系统回执](../../tmp/standard-case-stream-import-ikewlzh8/standard-case-stream-import.json)

PostgreSQL 与真实 SeaweedFS 也通过完整场景。整次请求的 Python 分配峰值为 4,686,224 B，节点预留归零。真实 API 和对象存储已退出，临时数据库已删除。S3 首轮在把打开的暂存流交给只接收路径的参数时失败。后续按目的地类型把路径或文件流传给同一复制函数，复验通过。[S3 回执](../../tmp/standard-case-stream-import-ow1r73lm/standard-case-stream-import.json) · [临时数据库清理](../../tmp/terminal-agent-postgres-hvogse9m/cleanup.json) · [首轮记录](../../tmp/standard-case-stream-import-ul3e9p15/standard-case-stream-import.json)

专项入口为 [standard_case_streaming_import_acceptance.py](../server/standard_case_streaming_import_acceptance.py)，使用本项独立 API、真实数据库和存储。占位脚本提供后续用户编辑入口，其余计算功能按各自原验收范围推进。[导入实现](../server/app/services/case_import.py)
