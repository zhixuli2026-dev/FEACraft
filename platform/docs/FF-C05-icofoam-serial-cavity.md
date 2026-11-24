# FF-C05 icoFoam 二维和三维基础求解

正式入口是 `POST /api/v1/cases/{case_id}/workflow-runs` 的 `target=solve`。B 阶段确认后实际生成并检查网格。C 阶段确认后，串行 `solve.openfoam` 绑定同一运行的 mesh publication，由安装的 OpenFOAM `icoFoam` 从 `0/` 读取 U 和 p。结果发布时检查运行来源、求解日志、输出时刻和场文件。D 阶段需要所有者的显式确认。[求解工作流说明](solve-workflows.md)

| 已执行基准 | 网格 | 终点 | 速度场输出时刻 | 实测残差 |
| --- | ---: | ---: | --- | --- |
| 2D 方腔 | 400 单元 | 0.5，100 步 | 0.1、0.2、0.3、0.4、0.5 | Ux 下降 38.565 倍 |
| 3D 方腔 | 27,000 单元 | 0.1 | 0.02、0.04、0.06、0.08、0.1 | Uz 有非零量测，p 下降 63,445.831 倍 |

两个基准均由真实 `blockMesh`、`checkMesh` 和 `icoFoam` 运行，经同一已确认的网格版本校验输入，读取实际体场并完成后续工程确认。三维结果沿用保存的原基准原始回执。[二维原基准](../../tmp/cae-registered-cavity2d-2o2s6vmw/benchmark.json) · [三维原基准](../../tmp/cae-registered-cavity3d-v0af8q46/benchmark.json)

二维恢复轮在实际写出 0.005 后取消，从 0.005 续算，写出 0.01 后再次取消，第三次从 0.01 继续到 0.5。相同步长下恢复与从头求解相比，400 单元的 U 和 p 最大绝对差均为 0。三维在 D 确认边界实际退出并重启 API，原工作流、待确认步骤和文件保持，经显式确认后延续原工作流。两组已归档，不重复执行 27,000 单元求解。[二维续算](../../tmp/workflow-checkpoint-FS-SQLite-ia8nvcr8/checkpoint-result.json) · [三维恢复](../../tmp/cae-registered-cavity3d-zn2fqiga/benchmark.json) · [实际进程重启](../../tmp/cae-registered-cavity3d-zn2fqiga/actual-api-restart.json)

2026-11-19 补验保留真实失败。将二维初始速度场的 `movingWall` 命名改错，实际 OpenFOAM v2406 读取 `0/U/boundaryField` 时退出。失败尝试保留原生诊断和非零退出码，未发布求解结果，D 阶段返回 409。[原生失败日志](../../tmp/icofoam-c05-ghkszg16/native-icoFoam-failure.log)

所有者按错误文件的 SHA-256 条件写修复后，从同一算例的新运行重新求解。实测结果仍为 400 单元、100 步、终点 0.5，Ux 残差下降 38.565 倍。五个时刻的十份 U、p 文件分别用实际 HTTP 下载并核对发布清单，且与 N7-03 二维原基准的对应文件逐字节一致。独立 VTK 读取了 400 个单元的有限速度和压力值。所有者完成 D 确认后，原失败工作流与日志仍按原记录可读。[补验回执](../../tmp/icofoam-c05-ghkszg16/icofoam-contract.json)

另一账号尝试修复初始文件、读取工作流、日志、发布和原始场文件、执行 D 确认，均返回 404。本次 4 项补验通过，7 个原生进程均有停止回执，隔离 API 已退出。真实模型和模拟模型调用均为 0。本次沿用当前生产入口，未更改求解器实现。[验收入口](../server/icofoam_contract_acceptance.py) · [审查清单](../../tmp/review-FF-C05-icofoam-contract-20261119.json)
