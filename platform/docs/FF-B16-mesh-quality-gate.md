# FF-B16 网格质量门禁

正式工程链通过 `workflow-runs` 运行网格生成，收到 B 阶段确认后才执行 `mesh.generate`。实际 OpenFOAM `checkMesh` 给出最大非正交角、最大偏斜度、最大纵横比和最小单元体积。`mesh.qualityTargets` 分别用 `maxNonOrtho`、`maxSkewness`、`maxAspectRatio` 和 `minVolume` 约束这四项指标。`B-mesh/quality.json` 保存实测结果、阈值、超限项、工程输入及源日志的 SHA-256。

配置 `minJacobian` 时，同一任务通过 OpenFOAM `foamToVTK` 读取生成后最终 `polyMesh` 的原生体单元，按 Verdict 的定义计算最小无量纲 scaled Jacobian。四面体、六面体、棱柱和金字塔采用各自的参考单元定义。遇到其它多面体且要求 `minJacobian` 时，任务说明无法测量并阻断。网格发布校验 VTK 体单元数与 `checkMesh` 一致，并从封存的几何与日志重新计算报告。历史版本仍可按其记录读回。[VTK 的 Jacobian 定义](https://vtk.org/doc/nightly/html/classvtkMeshQuality.html) · [Verdict](https://github.com/sandialabs/verdict)

`status` 表示量测有无越界，`gate.status` 说明工程决策。测量全部达标时为 `pass`。`fail` 策略阻断超限，`warn` 策略保留超限并允许进入 C 阶段。`allowWithReason` 在超限时必须记录非空理由。配置的指标缺失、`checkMesh` 未证明 `Mesh OK` 或当前证据失效时，阶段推进被阻断。被拒网格的日志和报告保存在任务的隔离尝试目录。

通过 `GET /mesh/results` 可查当前网格或指定历史发布。已存在正式网格发布时，`POST /mesh:evaluate` 验证并返回对应发布的量测报告。导入的独立日志仍可由没有正式网格发布的算例评估。当前网格与输入一致并完成 C 阶段确认后，求解任务才能绑定并使用该版本。

## 原生量测

| OpenFOAM 算例 | 最大非正交角 | 最大偏斜度 | 最大纵横比 | 最小 scaled Jacobian | 最小体积 m³ |
| --- | ---: | ---: | ---: | ---: | ---: |
| `blockMesh` 原方腔，400 个六面体 | 0° | 1.66533×10⁻¹⁴ | 1 | 1 | 2.5×10⁻⁷ |
| `blockMesh` 变形方腔，400 个六面体 | 27.1124° | 0.535002 | 1.824 | 0.887216801 | 2.52×10⁻⁷ |
| `gmsh` 经 `gmshToFoam` 转换，64 个六面体 | 0° | 0 | 1 | 1 | 0.015625 |

Windows API、SQLite、本机文件系统和受控非特权 WSL 跑通 11 项断言。独立 VTK 读取封存的实际体网格并验证 scaled Jacobian。变形方腔通过原生 `checkMesh`，五项指标均超工程目标，四种策略和理由组合行为符合合同。另一实际热算例未通过 `checkMesh`，`warn` 仍阻断求解启动。[正式验收结果](../../tmp/mesh-quality-b16-x6euv9mv/mesh-quality-results.json)

算例所有者从正常 API 提交任务、确认阶段与重跑。另一账号对同一报告、评估、证据文件和 C 阶段确认均得到 404。工作目录中的 VTK 证据改动后，确认与评估均拒绝，且没有启动求解。恢复原文件后，实际 `icoFoam` 绑定原网格版本并运行成功。修改目标后旧报告保留并标记过期，真实网格重跑失败触发 C→B，重新生成合格网格后可再次进入 C。[验收入口](../server/mesh_quality_gate_acceptance.py)

实际共 26 个原生作业，SQLite 尝试记录里均有验证通过的进程停止回执，包含四个预期失败的网格作业。隔离 API 已退出。真实模型调用 0，模拟模型调用 0。首轮夹具在异步回退完成前结束，第二轮等待实际回退状态，生产代码无变化。[首轮记录](../../tmp/mesh-quality-b16-x7l146kt/mesh-quality-results.json) · [第二轮登记清单](../../tmp/review-FF-B16-mesh-quality-gate-v2-20261119.json)
