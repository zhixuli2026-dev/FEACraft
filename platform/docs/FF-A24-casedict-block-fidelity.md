# FF-A24 caseDict 块级编辑保真

实际 Edge 提交整个 `notes` 块时只改 `marker` 的值。编辑后下载的 caseDict 除该值外逐字节相同，包括 CRLF、缩进、引号、列表、注释及带同名子块的其他顶层条目。原实现会在顶层目标前选中同名子块，还会重写未改的子块和列表并丢失注释。[修复前复现](../../tmp/FF-A24-before-20261119.json) · [Edge 回执](../../tmp/casedict-block-fs-njoa6hw3/edge-result.json) · [表单截图](../../tmp/casedict-block-fs-njoa6hw3/edge-block-editor.png)

块编辑沿用字典分词，按顶层有效条目定位。递归应用现有子块改动，保留其他值的原始 token 和相邻注释。全量编辑移除未提交的键，清空块保留它的边界和外围注释。严格局部模式继续要求目标块和键存在。新增标记沿用原文件换行和现有块缩进。带注释的阶段值仍可经专用确认入口更新。[实现](../server/app/services/casedict.py)

| 验收环境 | 实际检查 |
| --- | --- |
| Edge／SQLite／文件系统 | 全块表单仅改变一个值，第二次嵌套编辑只有目的值变动，清空保留注释，冷 API 读回版本和原字节。[实际回执](../../tmp/casedict-block-fs-njoa6hw3/block-fidelity.json) |
| PostgreSQL／SeaweedFS | 正式 API 重复验证顶层命中、相邻注释、CRLF、冷启动和原字节读回。[实际回执](../../tmp/casedict-block-s3-aic22hyp/block-fidelity.json) · [临时数据库回收](../../tmp/terminal-agent-postgres-f_sq5bdr/cleanup.json) |

两种部署均验证过期修订和错误摘要返回 409，跨账户写入返回 404，非法结构返回 422。这些请求前后文件与修订一致。正式 A→V 确认只改阶段值，修改算例名时带注释和空格的标签列表保持原文。另在直接路径验证空局部编辑、缺失块／键、删除重复键以及相邻字符串和裸词的边界。四次实际 API 进程退出均已核验。真实模型和模拟模型调用均为 0。[复验入口](../server/casedict_block_fidelity_acceptance.py)
