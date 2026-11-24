# 原任务取消后的独立校验收尾与跨 API 恢复

登记前缓存/日志的持久归属及后续终态补交进展见[收尾IO归属](finalizing-io-ownership.md)；本页历史运行与原失败保持。

2026-11-10（北京时间）。承接[错误后断连的恢复](validator-mixed-cancellation.md)。本段解决已登记独立校验请求后，用户取消原 Job，实际校验停止却无法完成原 attempt 收尾的问题；同时验证两个实际 API 竞争同一次恢复。证据对账见 `tmp/mainline-N5-04-job-cancellation-evidence/validation.json`。

主线仍为 **37/53，剩16项**；资源预算工作表仍为11/17、剩6块。本段未完成整个 N5-04，原四条 remaining 和295项全功能目标保持。

## 实际反例与修复

`tmp/professional-finalizing-ibp4wjnl/professional-result.json` 中，原报告真实编译后遇到实际 S3 上传中断，原 API 退出；恢复 API 从归档重建缓存并启动独立校验。实际编译完成、受监督进程仍运行时，通过正式 `POST /jobs/{id}:cancel` 取消原任务，收尾 HTTP 连接保持。心跳检测到取消并停止真实进程，但旧收尾只接受 finalizing 或已有完整证明的终态，返回409；Job为cancelled，attempt永久停在cancelling，write_checkpoint为空，重复请求仍拒绝。原构建没有重放。该记录是修复前反例的通过，不是产品成功。

`finalizing_job_cancellation.py` 在原 Job 已取消、attempt 确有取消请求、原输出已归档且独立请求已登记时，核验原 worker 实际退出、独立校验实际停止、原请求绑定和原用量，接续原 attempt。沿用原 v5 完整排空证明、终态与发布事务；取消时产物预留标为failed，当前发布指针保持。使用已提交归档中的原日志，不新增缓存重建、上传或原生执行。

运行中的原生子进程必须保留实际 stop，未注册业务进程的准备状态必须提供独立的未放行证明。新增 `vibeflow.finalizer-preparation-closure.v3` 绑定已完成 NativeRequest 的实际 ended_at、原错误与完整请求摘要，记录真实 guardian/native 回收及已知 bootstrap 退出。它不声称 API 退出，不为历史 child 补造 stop，未知 bootstrap 出生仍明确未知。原 v1/v2 所有权恢复和 HTTP 取消证明继续使用各自授权。

普通原生放行仍要求原 Job 正在运行；只有取消收尾的快照允许确切的 Job.cancelled / attempt.cancelling 中间状态。实际完成时重新核验取消，防止把取消任务发布为成功。

## 当前实际验收

目录均为 `tmp/professional-finalizing-后缀/`，回执为 `professional-result.json`。

| 场景 | 数据库 | 后缀 | 第二个实际 API |
|---|---|---|---|
| 报告编译进程运行中取消原 Job | SQLite | 89t8wv24 | 无 |
| 报告 child 刚预留、业务进程未注册时取消 | PostgreSQL | t4iv7589 | 拒绝接管、发起取消、重复收尾 |
| LaTeX native_ready 处取消 | SQLite | tgh5cce5 | 拒绝接管、发起取消、重复收尾 |
| LaTeX WSL 资源准备完成时取消 | PostgreSQL | c7ml0lyn | 拒绝接管、发起取消、重复收尾 |
| 独立请求刚提交、尚无 child 时取消 | SQLite | vrx5jwuz | 拒绝接管、发起取消、重复收尾 |

五组均保持原 Job、attempt、输入与绝对截止时间，原构建各一次，最终 Job/attempt 都为cancelled，v5引用原实际 stop，原产物未成为当前发布。四组通过第二个 API 取消；它首先在真实 owner 仍运行时请求接管并获得409/recovery_required，之后两个实际 HTTP 客户端的重复收尾获得相同结果。带完整 Job 状态事件观察的 SQLite 请求提交组及 PostgreSQL WSL准备组还核验取消事件恰好一次。原生日志和额度不重置；未知额度耗尽/过期的组合仍在原剩余范围。

前两版验收观察字段不同，生产修复相同；分别绑定 v3/v4/v5/v6 源码审查记录，保留历史夹具摘要，不改写成当前夹具运行结果。审查见 `tmp/N5-original-cancel-review-v1..v7-20261110.json`，覆盖导入的辅助代码；v7修正封存时对取消停止回执的过强断言。

## 跨 API 竞争与真实模型

`tmp/professional-finalizing-qrbi61fh/professional-result.json` 通过显式入口执行：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B live_llm_mixed_finalizer_acceptance.py --source-profile-id 26 --postgres --two-apis
```

原实际报告完成独立编译后产生真实文件读取错误，随后收尾 HTTP 断连。两个独立 uvicorn API 使用相同隔离数据库与 S3，实际同时请求接续同一 attempt，得到200与409/state_conflict；最终只新增一个关联校验请求。旧请求、实际停止记录和原错误保持，原产物按原 publication 发布一次，原构建只执行一次。两个 API 再次请求返回相同成功结果。

随后真实 `api.deepseek.com` / `deepseek-flash` 选择一次 `read_text_file` 读取恢复的原报告源文件，并在真实后续流式回复中返回只存在于实际文件中的随机验证串。共2次HTTP200、2份usage、8558 tokens；原HTTP/SSE转发，模拟LLM0。证据为同目录的 `upstream.jsonl` 和 `actual-recovered-model-events.json`。授权凭据从本地profile读取，不输出密钥。

## 失败记录与基础设施

两份验收脚本失败原样保留，均不计为产品通过：`o3dlkwlw` 读取不存在的发布指针属性，在取消前失败；`tpgdk13q` 未处理尚无child的空记录，实际任务已取消，但观察断言KeyError。分别修正观察字段及空记录处理后重验。没有把这些返工计为新增功能，也没有通过固定原生或模型结果替代运行。

首次封存脚本 `tmp/checkpoint-N5-04-job-cancellation-20261110.py` 还错误地要求所有取消进程都有成功业务控制回执。LaTeX native_ready取消时，放行门实际退出125，原stop为verified、范围为空、native实际退出，但control_valid=false；既有停止协议允许这种非零退出并由独立kernel回收证明核验。该原stop保持不变。修正验收断言、用原完整停止协议核验已有真实回执后，采用新的`-v2.py`一次性封存；原失败脚本不重跑，不把它列为运行场景通过。

本段共9轮实际原构建：5组取消通过、1组跨API恢复及真实模型兼容、1组修复前反例、2组夹具失败。23个已观察实际 API 的出生身份均有原进程句柄或确切退出见证；9个自建 S3 owner实际退出；3个临时 PostgreSQL 库删除并独立查询确认。API/S3退出不用于补造旧阶段缺失的原生kernel停止证明。开发数据库未写入，未运行历史混合模拟LLM回归集合。

## 继续推进的原范围

独立请求登记前的缓存恢复/日志上传仍缺少完整跨API操作权证明；没有请求指针时，本段拒绝猜测这些操作已经排空。下一步补齐这个窗口的持久操作登记、取消和恢复，使该阶段也能安全接续。

其余原要求继续：diagnostic增长/未知输出额度耗尽/原期限到期组合、证据生成和同步在完整证明形成前中断、历史缺失物理停止证明的明确处置，以及其他处理器、FS、legacy和旧缓存/staging。原偶发SQLite锁失败的精确持锁者仍未定位。53项主线和295项全功能目标均保持有效。
