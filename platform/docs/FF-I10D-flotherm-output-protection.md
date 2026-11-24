# FF-I10D FloTHERM 输出与中断保护验收

2026-11-20。CLI 分步导入生成独立 IR 目录。`build` 拒绝相同或相互嵌套的算例与 IR 目录，拒绝输出包含原始输入。已有输出需要显式 `--force` 和完整生成标记。Windows 与非特权 WSL 均以实际命令核验同目录、嵌套目录、保留原 IR 文件和已有未登记内容。[CLI 与 OpenFOAM](../../tmp/flotherm-neutral-ecxml-20261120.json) · [WSL CLI](../../tmp/flotherm-neutral-ecxml-wsl-20261120.json) · [生成标记与强制重置](FF-I02-I10A-flotherm.md)

异步作业先封存输入，在私有 attempt 目录生成 `blockMeshDict` 等产物。成功后校验来源和实际生成清单，再发布到用户目录。实际 Edge 中从工作台提交取消并看到最终 `cancelled`，检查确认原文件保留、用户目标目录未发布。[Edge 验收](../../tmp/flotherm-studio-browser-9_gs3xs7/attestation.json)

真实 worker 完成生成后，在明确的测试检查点复验取消、18 秒执行期限和 API 进程退出。三组都核验所属进程范围已退出，外部哨兵进程仍运行，原输入摘要一致，私有半成品保持，用户目录未发布。最早的期限夹具只修改本地运行器，先观测到 guardian 结束进程。最终将同一个实际期限传入注册处理器与运行器，核验 `timeout` 停止回执。[停止回执](../../tmp/worker-scope-flotherm-zid8iul3/scope-receipts.json) · [验收入口](../server/professional_worker_scope_smoke.py)

SQLite、PostgreSQL 各 11 项真实发布回归通过，涵盖源和输出竞争、无效输入、产物和回执损坏、取消、API 重启后发布已核验结果。重启场景保持一个作业，发布原验证产物。重复请求返回同一作业，原件和用户添加的目标文件保留。另有认证 S3 的实际原始文件、条件保存、作业发布，以及先前的 S3 封存重试证据。[本次 22 项发布检查](../../tmp/flotherm-publication-evidence-dp4d09ss/result.json) · [认证 S3 原始模型](FF-I01-I07-ecxml-import.md) · [S3 既有重试与重跑](flotherm-udf-repeat.md)

以上本次验收模型调用 0，模拟调用 0。复杂几何与原始输入覆盖继续由 FF-I01/I04/I07/I08 跟踪。
