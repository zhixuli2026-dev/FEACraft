# 原生交接中断与独立期限

2026-11-07，继续 N4-05 和资源生命周期主线。此前[九位置取消矩阵](image-transport-handoff-cancellation.md)覆盖 API 回调及异步交接，本批进一步进入实际原生助手内部。

## 执行设计

显式入口 `live_llm_native_handoff_fault_acceptance.py --source-profile-id 26 [--postgres]`。只读复用已授权配置，开发数据库与 8000 端口不参与算例。退休基础算例、双 API 和镜像回收均调用实际服务；最终模型通过官方 DeepSeek 服务自行选择工具读取恢复文件。

原生故障使用隔离源码副本，仅复制服务 `.py/.sh` 和内置技能静态资源，记录原始及插桩文件哈希。只在副本 `native_process_preflight.py`、`process_gate.py` 加入阶段标记与实际 `os._exit(83)`，不修改生产文件，不替换握手、身份观测或模型响应。

| 场景 | 实际动作及要求 | 类别 |
| --- | --- | --- |
| before_permit | 发出只读出生握手后、读取许可前退出 | 实际基础设施 |
| after_permit | 验证真实许可后、进入原生范围前退出 | 实际基础设施 |
| after_join | 加入实际原生范围后、发出业务门握手前退出 | 实际基础设施 |
| wrong_permit | API 发出的许可带错误出生哈希，真实原生助手必须拒绝 | 协议故障 |
| preflight_deadline | API 暂停发送许可，等待助手原有 45 秒 SIGALRM 实际退出 | 实际基础设施 |
| guardian_deadline | 选择合法 20 秒守护期限，等待实际 guardian 到期关闭 Job 并形成 deadline 回执 | 实际基础设施 |

期限场景暂停的是 API 的实际 host_ready 回调，不缩短原生 45 秒期限，也不伪造 guardian 回执。所有者存活和回调排空前，另一 API 必须返回 409。每次失败后验证镜像、快照完整，范围已关闭，已知真实身份退出。全部场景后重试只完成一次清理，两台 API 返回同一结果，失败记录的 children/result 保持原样。

本批基础设施证据不代替模型验收：真实模型阶段未成功时保留整体失败，不回退模拟。

## 本批结果

- SQLite：[六场景及唯一重试](../../tmp/sandbox-recovery-ui-retirement-1jr8o9sx/native-handoff-checks.json)、[真实模型](../../tmp/sandbox-recovery-ui-retirement-1jr8o9sx/live-result.json)。6 项基础设施、1 项协议故障、1 项真实模型检查通过；2 次 DeepSeek 调用，8,412 tokens。原生助手退出观测约 44.968 秒，guardian 约 19.875 秒。
- PostgreSQL：[六场景及唯一重试](../../tmp/sandbox-recovery-ui-retirement-vi03__0r/native-handoff-checks.json)、[真实模型](../../tmp/sandbox-recovery-ui-retirement-vi03__0r/live-result.json)、[临时数据库删除](../../tmp/image-postgres-b53w6mc1/result.json)。同样 6/1/1 项通过；2 次 DeepSeek 调用，8,508 tokens。原生助手约 45.031 秒，guardian 约 19.984 秒。

合计 12 项实际基础设施、2 项协议故障、2 项真实模型验收，包含 6 次实际原生退出故障和 4 次实际期限到期。真实 `api.deepseek.com / deepseek-flash` 推理 4 次，全部 HTTP 200，共 16,920 tokens，模拟 0。两套模型均实际选择读取工具，恢复文件未修改，严格原样回复通过。18 个已登记出生均核验退出，未登记的故障助手另有逐案实际退出观测。两台 API 各已停止，PG 临时库已删除。

源码副本及清单：[SQLite](../../tmp/native-handoff-source-nao04746/source-manifest.json)、[PostgreSQL](../../tmp/native-handoff-source-c2s1pupq/source-manifest.json)。固定来源文件哈希和实际副本哈希随归档核对。生产实现本批未改变；此次补齐的是已有中断/独立期限机制的实际验收证据。历史模拟集合未运行，审查限制未放宽。基础退休算例自带的权限和损坏注入没有重复计入上述新增矩阵。

## 范围与后续

独立助手/守护期限不等于全请求统一预算。现有 NativeExecutor 的业务执行计时从最终业务放行后开始；同步客户端 `run_native` 的 socket timeout 为每次 I/O 的 `timeout + 90`，持续输出可以继续刷新等待。镜像清理另有 `run_sync(..., timeout=300)`，但不覆盖其之前的归档读取、额度准入与配置准备。前面的等待、数据库和准备阶段仍须纳入统一期限设计，不能将这三个计时直接相加后声称全请求已受控。此次不宣称完成该项，也不宣称验证默认 360 秒守护配置的完整实时时长。

下一实施顺序：先给客户端和原生执行队列/准备/运行共享单调截止时间；超时后停止新的许可，保留实际创建与已提交预留，再排空其清理。随后将上游请求的归档、存储准入、数据库等待和最终提交纳入同一预算。必须覆盖“提交完成但调用者尚未收到返回时超时”的责任保留，不能只在外围添加一个可取消等待而遗失真实资源。

此前偶发退出核验拒绝和命令时延、WSL/宿主重启、实际空间与权限故障、跨节点全局额度以及其余 53/295 计划继续。主线 33/53、剩 20；资源 11/17、剩 6。
