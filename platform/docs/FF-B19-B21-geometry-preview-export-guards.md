# FF-B19 与 FF-B21 几何预览的导出保护

2026-11-19，在隔离 SQLite 和文件系统上使用 Windows CadQuery 2.8.0 与 OCP 7.9.3.1.1 完成 10 个实际作业。自动保存预览、手动预览与工作流默认预览均生成范围正确的场景。手动预览采用 0.15 精度，默认预览工作流经确认进入 B 阶段。

预览脚本在顶层调用 `cq.exporters.export`，在 `build_geometry` 内调用通过 `from cadquery import exporters` 取得的别名。两个位置均在实际作业的私有目录内断言目标文件的存在状态与字节保持，并记录各自执行检查点。公开算例中三条自定义导出路径均返回 404，预览工作流的发布也没有 STEP/STL。即使声明了调用后会抛错的 `export_geometry`，自动预览仍成功，日志没有钩子执行记录。

显式手动导出和显式工作流导出各调用 `export_geometry` 一次。两条内联路径均生成真实 STEP，钩子产物可以读回。正式 STEP 文件的首尾标识完整，STL 实际顶点的宽度为 7，下载文件与各自发布摘要相符。手动导出的清单正确绑定对象与 STEP/STL。正常导出工作流经确认进入 B 阶段。

两个故意失败的作业分别在预览构建后与导出钩子内报错。旧场景可读，旧 STEP/STL 的摘要在后续预览与恢复后保持，失败路径没有发布新文件。修正脚本后自动预览成功。其它账户发起预览、发起导出与读取旧发布均收到 404。全部十个作业的实际进程停止经过核验，隔离 API 的原进程已独立确认退出。真实模型调用和模拟模型调用均为 0。[结果](../../tmp/geometry-script-contract-_2dtrd2_/geometry-script-contract.json) · [独立退出核验](../../tmp/geometry-script-contract-_2dtrd2_/cleanup-independent.json)

首轮在第一个实际作业导入 CadQuery 时失败。CadQuery 2.8.0 从包属性暴露 `exporters`，夹具随后改用该公开导入形式并完整复验。本轮新增了验收覆盖，现有几何执行器沿用原实现。[首轮失败](../../tmp/geometry-script-contract-ffniks1t/geometry-script-contract.json) · [复验入口](../server/geometry_script_contract_acceptance.py) · [审查散列](../../tmp/FF-B19-B21-geometry-export-guards-20261119/source-review.json)
