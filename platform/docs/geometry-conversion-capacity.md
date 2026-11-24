# 上传几何文件转换的锁范围与资源预算

`POST /geometry/repair`、`POST /geometry/msh-preview` 和 `GET /geometry/import-scene` 共用每个 API 进程两个转换名额。名额用完立即返回 HTTP 429 和 `Retry-After`。整个请求共用存储操作期限，覆盖源版本选择、准备、转换和写入。源文件继续限制为 16 MiB。源复制每次读取 64 KiB，并验证实际文件身份或对象版本及长度。[转换服务](../server/app/services/geometry_conversion.py) · [几何入口](../server/app/routers/geometry.py)

读取源版本后结束算例事务及账户准入。同一算例的普通写入可以在 CAD 或 VTK 转换期间完成。发布前重新验证权限及源版本；源被删除或替换时返回 `input_stale`。默认禁止覆盖的修复在初选与提交处都检查目标不存在。修复的 STEP 和验证报告继续使用同一个文件事务，Gmsh 的 VTU 路径继续由源文件路径与原字节决定。只读共享成员可以查看 STEP 场景，两个写入入口仍要求编辑权限。

临时目录在实际节点存储卷上创建。节点准入计入源文件及写入入口的现有单文件事务上限，转换失败、超时或返回后清理临时目录及预留。CadQuery、meshio 与 VTK 在受限名额的工作线程里执行。期限到达后，服务等待手头的原生调用退出，再返回 HTTP 504，期间保留名额且拒绝过期产物。原生代码级的强制停止、多个 API 进程的总 CPU 限额与实际驻留峰值继续由 N4-05 跟踪。[资源工作清单](resource-budget-worklist.md)

## 2026-11-19 验收

四种组合使用真实 CadQuery、meshio 和 VTK。导入的原始长方体通过包围盒检查，修改后的 STEP 放大至体积 192，转换后的 VTU 由 meshio 读回一个四面体。验证报告中的 SHA-256 与转换源字节相同。另核验共享只读成员可导入、无权修复、默认拒绝覆盖、显式覆盖、节点容量不足拒绝以及格式错误的 Gmsh 请求返回后 API 仍正常。[专项入口](../server/live_llm_geometry_conversion_acceptance.py)

故障屏障等待真实原生转换完成后暂缓工作线程返回。两个名额均在使用时，第三个转换返回 429，同一算例可保存独立文件并替换原 STEP。旧修复返回 `input_stale`，两个目标均未创建。单独缩短请求期限后，过期线程仍占用名额和临时目录，释放后返回 504，同时清理临时目录。节点额度恢复后重试正常发布。[故障检查](../../tmp/live-cli-startup-n09l0aje/geometry-conversion.json)

| 数据库与存储 | 工程数值、并发与恢复 | 模型读取 |
| --- | --- | --- |
| SQLite 与文件系统 | [结果](../../tmp/live-cli-startup-zuusotc3/geometry-conversion.json) | [回执](../../tmp/live-cli-startup-zuusotc3/live-result.json) |
| PostgreSQL 与文件系统 | [结果](../../tmp/live-cli-startup-n09l0aje/geometry-conversion.json) | [回执](../../tmp/live-cli-startup-n09l0aje/live-result.json) |
| SQLite 与 SeaweedFS S3 | [结果](../../tmp/live-cli-startup-3oayz6vo/geometry-conversion.json) | [回执](../../tmp/live-cli-startup-3oayz6vo/live-result.json) |
| PostgreSQL 与 SeaweedFS S3 | [结果](../../tmp/live-cli-startup-9xg7oy4j/geometry-conversion.json) | [回执](../../tmp/live-cli-startup-9xg7oy4j/live-result.json) |

最终四组由真实 `deepseek-flash` 通过文件工具精确回读结果，8 次 HTTP 200。更早五组通过记录合计另有 10 次真实 HTTP 200，本阶段共 18 次，模拟 0。首轮包围盒断言改为使用 `1e-6` 公差。PostgreSQL 还出现账户准入和提交后清理的 409，最终复用现有暂等逻辑及普通文件提交后的清理顺序后通过。失败日志保留。[包围盒初测](../../tmp/live-cli-startup-vesxp82n/live-result.json) · [PostgreSQL 争用记录](../../tmp/live-cli-startup-8jb3vq86/live-result.json)
