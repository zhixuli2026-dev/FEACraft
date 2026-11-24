# FF-A17 几何声明与拓扑验证

几何预览和导出将 `sourceType`、`topology`、`scaleToMeters`、`symmetryUsed`、`periodicUsed`、`watertightRequired` 和 `manifoldRequired` 记入验证文件。发布前按封存的 caseDict 核对这些声明，并保留原几何脚本和工程输入摘要。几何面板可以查看同一发布的配置、实测水密和流形结果、边界边及非流形边计数。核对原结果时读取固定发布，也可以主动切换到最新结果。

算例明确要求水密或流形时，阶段 B 按相应实测结果核验。已有可查看的几何发布继续可读，未明确声明要求的开放几何沿用原阶段规则。旧验证记录缺少声明时，阶段核验使用与验证输入摘要对应的 caseDict。比例、对称和周期字段记录作者的配置，原有几何缩放、镜像及周期复制通过明确参数触发。

## 2026-11-19 实测

- CadQuery 开放面有四条边界边，流形检查通过。未声明水密要求时，明确确认 B 成功。声明水密后重新预览，几何再次有效发布，但确认 B 返回 409，阶段和字典修订保持。另用原生 OCCT 缝合三个具有公共边的面，测到一条非流形边，该边有三个邻接面。共享检查函数据此拒绝流形要求。[拓扑和阶段回执](../../tmp/geometry-declarations-browser-rs7_pr29/geometry-declarations-result.json)
- 真实 Edge 读取开放面配置和实际测量，核对原发布在换用闭合实体后仍可读且标记过期。切换最新发布后，实体的水密和流形都通过，边界边为零。实际导出实体体积 6，验证中的脚本摘要与正式工程输入一致，随后明确确认 B 成功。其它账号读取固定发布返回 404。[浏览器回执](../../tmp/geometry-declarations-browser-rs7_pr29/browser-result.json) · [闭合实体验证画面](../../tmp/geometry-declarations-browser-rs7_pr29/closed-geometry-evidence.png)

本轮在 D 盘隔离的 SQLite 和本机文件存储完成四次真实 CadQuery 预览或导出，并运行原生三面共边检查。模型调用为 0。既有实际几何修复涵盖缝合、修复、非对称镜像、周期复制、均匀和非均匀缩放及 STEP 原件。历史多存储测试涵盖参数变化、发布产物读回与保留旧结果。大型模型性能依据原计划另外验收。[既有修复验收](progress.md) · [几何发布和参数验收](N3-02-acceptance-audit.md)

## 存量修复产物

只读核对历史算例 2834 的三份报告。它们记录的输入 SHA-256 均与实际源 STEP 一致。旧镜像和非均匀缩放记录来自原生 CadQuery 对象断言，分别检查非对称实体质心和体积、XYZ 缩放后体积和包围盒。[历史源 STEP](../server/data/files/workspaces/1958/cases/2834/A-geo/exports/result.step) · [原生修复断言](../server/geometry_repair_smoke.py#L57) · [真实历史运行日志](../../tmp/mainline-N2-professional-evidence/write-compatibility-final/geometry_repair_smoke.log)

| 历史操作 | 存量原件和记录 |
| --- | --- |
| 修复 | [STEP](../server/data/files/workspaces/1958/cases/2834/A-geo/exports/repaired-fix.step) · [验证](../server/data/files/workspaces/1958/cases/2834/A-geo/exports/repaired-fix-validation.json) |
| 均匀缩放 | [STEP](../server/data/files/workspaces/1958/cases/2834/A-geo/exports/repaired-scale.step) · [验证](../server/data/files/workspaces/1958/cases/2834/A-geo/exports/repaired-scale-validation.json) |
| 周期复制 | [STEP](../server/data/files/workspaces/1958/cases/2834/A-geo/exports/repaired-periodic.step) · [验证](../server/data/files/workspaces/1958/cases/2834/A-geo/exports/repaired-periodic-validation.json) |

当前补验的存储配置为 SQLite 和文件存储。正式阶段 API 验证了非水密开放面的拒绝。流形拒绝通过原生三面共边样本和同一检查函数验证。

专项入口为 [geometry_declarations_browser_acceptance.py](../server/geometry_declarations_browser_acceptance.py)，通过项目的回归审查登记执行。
