# 下游任务引用已发布产物

N3-06第四批增量：普通HTML/DOCX与独立compose已统一注册发布，移除旧内存latest/直接写稿分支；保留旧证据门禁、覆盖和原字节备份，补齐固定生成时间、两代幂等键、历史和跨API验收。普通文档双库各23项、工程report各23/compose各25、core14/契约4/最终兼容3通过，见 [ordinary-document-workflows.md](ordinary-document-workflows.md)。主线仍9完成/14实施中/30待实施，295项功能范围不变；下一单元N4-01进程身份，N3-06完整取消/恢复前置尚未关闭。

N3-06第三批增量：全部LaTeX入口已接通注册编译、动态资源封存、按主文件持久版本、PDF/SyncTeX/BBL内容验证与历史源码定位，双库各32项通过，见 [latex-workflows.md](latex-workflows.md)。HTML/DOCX各23项、compose各25项复验；主线仍9完成/14实施中/30待实施，295项功能范围不变。这一历史增量后的普通文档统一已由第四批完成；完整取消/恢复前置仍在N4/N5推进。

N3-06第二批增量：独立报告草稿已接入注册双通道、原字节备份、覆盖保护、持久历史与HTML/DOCX生成来源，双库各25项通过，见 [report-compositions.md](report-compositions.md)。HTML/DOCX双库各23项复验；主线仍为9完成/14实施中/30待实施，295项功能范围不变。接下来继续LaTeX注册，N3-06整项未关闭。

报告HTML/DOCX消费者已接入postprocess全部产物及mesh质量，连同工程配置、用户正文/图片封存；自动正文保留用户稿。输入和文档内容复验后持久发布，见 [report-workflows.md](report-workflows.md)。

后处理消费者已接入solve全部产物与其mesh依赖，连同当前参数和独立参考一并封存；完整指标/图件复算后发布。E及报告门禁复核当前head、递归来源与工作副本，见 [postprocess-workflows.md](postprocess-workflows.md)。

求解消费者已接入：polyMesh来自已确认mesh publication，递归复核几何来源；原始场和system/constant参数另行封存。D确认还核对工作副本网格，避免旧后处理入口读到被替换网格。见 [solve-workflows.md](solve-workflows.md)。

后续网格接线：current引用会递归检查已登记上游publication（循环或超过16层拒绝）；几何head变化使网格current消费失效，显式历史仍按原版本封存。证明见 [mesh-workflows.md](mesh-workflows.md)。

2026-11-02。Job API、内部步骤队列和受限 shell 重试可将已有发布版本作为封存输入。该增量继续推进 N2-03/N2-04/N2-05；专业网格/求解/后处理/报告处理器和整个依赖图尚未全部接通。

## 请求契约

`POST /api/v1/cases/{id}/jobs` 新增可选 `input_publications`：

```json
{
  "type": "mesh",
  "command": "your-installed-mesher-command",
  "runtime": "wsl",
  "input_paths": ["0-caseDict/caseDict", "C-run/case-1"],
  "input_publications": [
    {
      "publication_id": "具体几何发布的UUID",
      "paths": ["A-geo/exports/box.stl"],
      "require_current": true
    }
  ]
}
```

这里的命令是待调用方替换的示意，不是已安装工具或完整网格配方。通用 Job 即使指定 `type=mesh` 也不自动构成专业成功证据。专业 handler 仍须验证真实网格、质量政策及阶段证据。

- 最多选择16个不同 publication；`paths` 可选择该清单中1–256个文件，省略表示选中整个发布清单。不接受目录、越界路径、未列入清单的文件或执行元数据路径。
- 引用必须属于当前 case，关联 Job 必须成功，publication 必须为 committed 或有已验证产物的 superseded。未知和其他 case 的 ID 返回同样的不可用响应；没有当前 case 权限时仍按原授权规则拒绝。
- `require_current` 默认 true，必须为 JSON 布尔值。提交时要求该版本仍是活动 head，且其工程输入仍有效。当前已注册的 freshness 校验器为 geometry；后续产物类别须接入对应校验器后才能使用这一默认策略。
- 显式设置 false 表示有意使用历史版本，允许读取旧 committed/superseded 版本。不会隐式退回历史，也不会自动替换为最新版。历史引用仍须通过归属、成功状态、清单和所选文件完整性校验。
- 可以只提供 `input_publications`，无需普通 `input_paths`。文件保持其发布清单中的相对路径。不同版本或普通输入出现重名、文件/目录冲突、大小写别名冲突时拒绝，不静默覆盖；目前没有目标路径重映射。
- `input_paths` 和引用文件合并计入10000文件/1GiB上限，并在创建副本前检查两份复制空间的存储配额。既有 `run_dir` 求解证据仍禁止与隔离输入混用，须等待专业版本化 handler。

幂等请求哈希包含规范化的 publication ID、路径选择和 current 策略。旧请求未提供此字段时哈希保持不变。相同键重放原任务，即使当前 head 已变；同键修改版本、选择或策略返回409。

## 封存、执行及失效

调用方在 case 控制锁内，从 DB 读取版本归属和状态，核对发布 manifest hash、身份绑定与所选产物 hash。复制来源为 `.vibeflow/publications/{id}/files`，不读取可变的 `A-geo` 兼容文件。副本依旧使用独立 inputs/run 和无覆盖目录提交，源变化或准备失败仅清理本次持有目录。

`JobAttempt.input_manifest_json.workspace.publications` 为每个来源记录：

| 字段 | 含义 |
|---|---|
| publication_id / kind / generation | 产物版本身份与类别 |
| attempt_id / manifest_sha256 | 产物生产尝试及其清单的校验依据 |
| paths | 实际选中的文件，省略选择时也展开登记 |
| require_current | 本次请求的时效策略，重试不会擅自放宽 |

文件大小/hash/可执行标记进入合并后的 workspace.files；来源链不依赖文件名猜测。Job 详情沿原 case 归属检查返回 workspace 和 outputs。

执行有三处复核：

1. 原子 claim 后、启动原生进程前，校验封存副本和所有发布依赖。当前版本已换、输入已过期或产物损坏时，记录 failed/input_stale，不启动本次原生进程。
2. 原生进程结束后捕获输出，再复核来源。运行中依赖失效时保留独立 run 和输出清单，记录 `dependency_error=input_stale`，任务失败。
3. 最终 Job 终态事务持 case 锁，再复核一次，覆盖“输出清单已捕获，但成功尚未提交”的竞争窗口；必要时更新输出依赖状态及持久事件。已经提交的终态不会因以后版本变化而被迟到的重复回调改写。

这不意味着排队到原生启动之间的整个时间段都锁住用户编辑。已封存输入可以保证执行使用指定字节；后续改变由收尾复核阻止成为当前成功成果。当前没有监听依赖变化后立即终止运行进程，完整取消归 N4。

内部 `queue_step(..., input_publications=...)` 使用同一流程。受限 shell 重试复制旧封存输入，保留 publications 来源与策略、创建新 attempt/日志，不重新读取工作副本。current-only 来源已经失效时拒绝分配重试；需要历史复算时应明确创建新的历史请求。工程阶段确认规则保持不变。

## 验证与剩余工作

`server/publication_input_smoke.py` 在临时 SQLite/本机临时 PostgreSQL 上运行，测试通过 ASGI HTTP API、真实 CadQuery STEP 读取和真实 WSL 子进程验证；本套不声称执行了完整网格/求解流程。结果收集于 `tmp/mainline-N2-publication-input-evidence/`，最终结果为 `publication-input-result.json` 双库各19项、`geometry-publication-result.json` 双库各16项、`attempt-workspace-result.json` 双库各23项；`compatibility/summary.json`4/4、`execution/summary.json`3/3。临时PG已删除，开发库仍13表/旧head，未迁移或重载API。原始来源、两次测试夹具修正及最终边界补验见 `evidence-origin.json`。

在项目根执行：

```powershell
& platform/server/.venv/Scripts/python.exe -X utf8 -B platform/server/publication_input_smoke.py --postgres
```

默认只使用临时 SQLite。`--postgres` 仅接受本机 PostgreSQL，创建并在 finally 删除唯一命名的临时库；不升级开发库。新增脚本纳入 offline profile。其余 Job、尝试隔离、几何发布和批次兼容性分别复验，实际范围单独记录，不能把定向回归写成全部测试已通过。

仍待完成：其他专业 handler 的版本化输入/输出与工具链；几何→网格→求解→后处理→报告全图的重算范围和展示；历史路径重映射/跨case受控导入；publication保留策略、源版本回收后的恢复规则；提交日志/崩溃补偿；S3、多worker进程监督和前端版本选择。目前即使已有封存副本，引用的发布控制记录与原版文件仍须保留供重新校验。

全部范围仍以 [主线计划](next-stage-mainline-plan.md)、[全功能计划](full-function-delivery-plan.md) 为准。源发布契约见 [geometry-publication.md](geometry-publication.md)，副本与权限边界见 [attempt-workspaces.md](attempt-workspaces.md)。
