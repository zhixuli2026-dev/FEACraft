# N4-05 后处理报告门禁的大文件验证

旧版报告门禁先核对证据清单中的输入和产物，再单独核对 caseDict 中配置的参考文件。第二次核对保留原有顺序和参考文件专属错误提示，沿用证据清单的分块 SHA-256 读法。文件读取绑定已观察到的版本，只有摘要和读取结束后的当前版本都匹配，参考文件才通过。这样仍能发现首次核对结束后发生的修改。

已注册的后处理报告门禁原来把整个 S3 求解目录下载到本地，再列举 OpenFOAM 输入和时刻目录。现在使用原有的 S3 目录选择和校验规则直接读取元数据，按原有规则筛选并比较路径，再读一次元数据以识别期间变化。结果就绪、兼容文件和清单中输入的 SHA-256 仍由原有门禁校验。文件系统沿用既有目录扫描。

## 验收范围

`platform/server/postprocess_gate_streaming_acceptance.py` 使用隔离的 SQLite API，分别在 Windows 文件系统和带认证的实际 SeaweedFS 上运行。旧版算例保存实际 caseDict，并安装冻结的兼容证据清单，覆盖 40 MiB 参考文件以及两次核对之间的外部替换。已注册算例由 WSL 中真实的 OpenFOAM `blockMesh` 和 `laplacianFoam` 生成数据，经公开的 `POST /postprocess:run` 和 `GET /postprocess/results` 提取并验证 350 K，随后测试 40 MiB 无关对象、增加求解时刻和 S3 目录读取之间的外部替换。

`tracemalloc` 记录的是单次旧版门禁或单次已注册目录扫描的 Python 峰值分配。整个已注册报告请求还会重新核对上游发布的输入，那里仍可在必要时建立源文件视图。变更和测量仅覆盖后处理报告门禁的两处读法。

## 2026-11-19 实测

| 实际后端 | 40 MiB 参考文件门禁峰值 | 含 40 MiB 无关对象的已注册目录扫描峰值 | 验收记录 |
| --- | ---: | ---: | --- |
| Windows 文件系统 + SQLite | 554,326 B | 58,851 B | [文件系统结果](../../tmp/postprocess-gate-fs-fqcqkayt/postprocess-gate-streaming.json) |
| 带认证的 SeaweedFS 4.52 + SQLite | 472,761 B | 98,245 B | [S3 结果](../../tmp/postprocess-gate-s3-f29m_ou7/postprocess-gate-streaming.json) |

两组都发布并重新读取了温度均值为 349.999999944 K 的真实提取结果。旧版门禁在两次核对之间发现参考文件替换，保留参考文件的过期提示，下一次从证据清单核对阶段返回计算输入过期。已注册门禁识别新增的 `C-run/conduction/77/T`。S3 扫描在两次目录读取中取得 19 条有效路径，文件正文 GET 为 0。并发替换无关对象触发 `input_stale`，文件正文 GET 仍为 0。

两个隔离 API 的原进程已确认退出，隔离 SeaweedFS 已停止。测试目录和服务数据均在 D 盘。验收前后 C 盘可用空间分别为 22.21 GiB 和 22.21 GiB（四舍五入到两位小数），阶段内模型调用和模拟模型调用各 0 次，Docker 操作为 0 次。
