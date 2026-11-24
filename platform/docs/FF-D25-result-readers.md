# FF-D25 通用工程结果读取

从算例文件树可直接打开 VTK、VTU、VTP、VTM、XDMF、XMF、Exodus、STL 和 ParaView series。Exodus 扩展名包括 `.exo`、`.e` 和 `.exodus`。浏览器通过当前算例的授权可视化会话读取和渲染。[文件树路由](../web/src/shell/openProjectFile.ts) · [实际读取器](../server/app/services/result_readers.py)

XDMF 和 Exodus 从 VTK 的数据管线取得实际时间步。打开结果时工作台显示最近一步，选择其他时刻时点场和单元场随实际输入更新。直接调用读取函数时保留原有默认时刻。已有 series 仍默认读取索引中最近的成员。静态 legacy VTK 继续按静态数据展示。

复合结果默认渲染全部数据块。在「数据信息」中选择叶块可显示它的原生网格、边界和字段。「全部数据块」恢复原来的组合视图。当时间序列的下一步没有原选中的叶块时，界面显示这一时间步的完整数据并给出提示。ParaView 的「读取源数据」仍检查实际文件整体。

大型 VTP 与普通结果共同使用同一文件树入口。多用户并发、长时内存和交互延迟由 D22 验收，逐片流式渲染需另有规模和格式证据。[交付计划](full-function-delivery-plan.md)

## 实际验收

2026-11-19 在隔离 SQLite、本地算例存储和 Edge 157 通过工作台文件树打开真实数据。常规矩阵覆盖 11 个扩展名，并补一个混合时序和一个较大 VTP。共 13 个原生会话全部核验停止并释放范围，隔离 API 已退出。验收退出码为 0，浏览器错误和模型调用均为 0。[完整验收回执](../../tmp/viz-formats-d25-yixaevmi/result-formats-result.json) · [现行验收入口](../server/viz_result_formats_browser_acceptance.py)

- 两个实际时间步 0.25 和 1.75 在 XDMF、XMF、Exodus 三种扩展名和 series 上均可切换。压力范围为 [1, 4] 与 [11, 14]，单元温度为 20 与 30。独立 ParaView 5.11.2 读取了当前 XDMF 和 Exodus 的 0.25 步。二进制 legacy VTK、VTU、VTP 和 STL 的静态时间状态正常。[时序截图](../../tmp/viz-formats-d25-yixaevmi/result-exodus-time.png) · [各格式回执](../../tmp/viz-formats-d25-yixaevmi/result-formats-browser.json)
- VTM 默认显示 12 个点和 12 个表面单元。三个叶块各读取 4 点、1 个四面体。X 范围分别为 [0, 1]、[3, 4]、[8, 9]，压力范围也分别核验。恢复全部后重新显示 12 个单元。把混合 series 切到缺失选中块的时间步时，实际恢复完整源。[全部块画布](../../tmp/viz-formats-d25-yixaevmi/result-composite-all.png) · [独立叶块画布](../../tmp/viz-formats-d25-yixaevmi/result-composite-bracket.png)
- 复用 D15 的 45,960,682 字节 VTP，按原 SHA-256 核对并走普通分块上传和文件树。实际渲染读到 522,242 点、1,044,480 单元，VTK 报告该网格内存 53,046 KiB。D04 此前还验证过 130,050 点、260,096 单元的带字段 VTP。[52 万点球面](../../tmp/viz-formats-d25-yixaevmi/result-large-surface.png) · [较大输入来源](FF-D15-completion-review.md) · [D04 证据](FF-D04-viz-information.md)
- 重复时刻、不存在的原生时刻、缺失结果和不支持的格式均有拒绝证据。异主启动 XMF 返回 HTTP 404。本轮保留相同小样本的修复前读数，显示读取器曾丢失实际时刻，Exodus 未读出压力和单元温度。[修复前后 VTK 读数](../../tmp/ff-d25-reader-20261119/diagnostic-format-read.json)

使用上述读取器和渲染器复跑 D16，两个数据源的书签、撤回、重开和原生 PNG 摘要均通过，三个会话完成范围核验。[相机专项复验](../../tmp/viz-camera-d16-xcithfz_/camera-bookmarks-result.json)
