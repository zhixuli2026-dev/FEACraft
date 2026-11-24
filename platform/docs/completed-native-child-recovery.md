# 已完成原生子步骤与后续失联的恢复

2026-11-16，N4-07 增量；全部 53 项主线和 295 项全功能交付目标保持。

## 问题与修复

同一 attempt 可以顺序启动多个原生 child。旧 `job_native_scopes.recover` 遇到已有 `RETIRED` 的范围直接跳过，但 `job_scope_recovery` 又要求完整 native child 清单都有停止证明。因此前一步已经正常停止，后一步失联时，前一步反而成为恢复缺口。

现在保留并复用该范围**原本已经提交的撤销回执**，不再次发出撤销命令。原 owner、Windows guardian、host 范围和 native gate 退出仍独立核验；检查点必须包含全部原范围，并要求复用的回执与原历史逐字段相同。原 child 的 stop 存在或缺失也必须与检查点标记一致。没有把“回执结构合法”当作“就是原回执”，也不补写缺失的原结束记录。

LaTeX 处理器同时加入已有的注册 Python 程序路径映射。实际构建仍在 Windows/WSL 上运行；Linux 校验原 Windows sealed contract 时使用注册程序映射，原输入、原 argv 和原执行历史不改写。

## 实际流程

入口 `platform/server/live_llm_completed_native_child_acceptance.py`，审查 `tmp/N4-07-completed-native-child-review-v1-20261116.json`，共 623 个入口与传递依赖源码散列。使用实际 PostgreSQL、S3、Redis、Windows worker、WSL LaTeX 和 Linux worker。

| 故障窗口 | 原事实 | 要求的恢复结果 |
|---|---|---|
| LaTeX 探测已 stopped，后续编译 child 已完整准备但尚未 running | 原探测 stop 和 RETIRED 齐全；编译 child identity/stop 缺失 | 复用原探测回执，独立撤销编译范围，原 child 日志逐字保留 |
| LaTeX 探测 RETIRED 已提交，child finish 尚未提交 | 原撤销回执齐全；child 仍 running、stop 缺失 | 复用原撤销回执，保留缺失 stop，不伪造原结束回调 |

两条路径都由真实生产函数提交后再 `os._exit(17)` 退出原 worker，新 worker 根据原记录完成 interrupted 收尾。原任务只有一个 attempt，原输入、发布绑定、根身份和 child 日志不变，原公开 PDF 字节不变。

随后必须实际重新构建 PDF，校验新文档正文；平台自身仍执行独立 LaTeX 编译和 PDF/SyncTeX 等产物校验。再由 Linux 任务发布实际新 PDF 的摘要，官方 DeepSeek 通过真实文件工具读取该内容。模型答复不固定、不模拟。

Windows 与 Linux 分别只读校验同一实际检查点。除原有范围完整性检查，还验证：改变 `original_stop_missing` 必须拒绝；即使替换后的撤销回执单独通过结构校验，只要与原历史不一致也必须拒绝。纯凭据检查与真实模型调用分开统计。

## 剩余范围

本次补齐原范围已经撤销后的恢复，不代替未完整分配/入组/观察的更早窗口、其余外部 owner 及完整故障矩阵。N4-05 的资源预算和 N7 的部署、回归、全功能交付评审继续；不增加权限层或退出条件。

## 最终结果与归档

2026-11-16 N4-07 已完成原生子步骤衔接恢复已修复：native.recover 不再跳过原 RETIRED 范围，复用原撤销回执并独立核验 owner/guardian/gate 退出；检查点核对原 stop 缺失事实和逐字段原回执一致性。真实LaTeX探测已stopped后编译prepared失联，以及原RETIRE提交后child finish缺失，两窗口均恢复；原child日志/stop不改写，旧PDF保留，新PDF正文及独立编译验收、后续Linux任务成功。LaTeX沿用已有注册程序映射通过双主机凭据校验。共12个实际任务、15个原生子执行、2次官方DeepSeek deepseek-flash HTTP200、模拟0；50项纯凭据检查另列，59个记录原进程身份已消失。专属Redis/S3停止、临时PG删除及访问规则恢复。见 completed-native-child-recovery.md。主线48/53、剩5，295项交付状态保持。更早不完整准备、其余外部owner及N4-05/N7原范围继续。

实际证据 `tmp/remote-worker-i2m64vf1`，数据库清理 `tmp/remote-worker-postgres-e354123637c14abbb309eb13dd467833`。源码归档 `platform/backups/mainline-N4-07-completed-native-child-20261116.zip`，校验记录 `tmp/mainline-N4-07-completed-native-child-evidence/validation.json`。当前所有执行源码与真实验收审查散列一致。
