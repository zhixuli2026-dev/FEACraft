# WSL CLI 私有目录的所属初始化

2026-11-03，N4-03.h 后续增量。原 `codex_wsl_state.prepare` 在 CLI 持久记录创建前直接调用 `subprocess.run(wsl.exe …)`；15 秒宿主超时不能证明原生初始化进程已退出。本批移除该进程入口，把固定初始化函数放入已登记的 Linux stdio bridge 内执行。

## 启动顺序与失败边界

1. 平台先冻结目录根、用户/凭据版本对应的相对路径、配置正文和所有者描述；此步骤不启动 WSL helper、不写 Linux 私有目录。初始化描述进入 CLI selection 和通信 profile 摘要，初始化代码 SHA 也进入选择记录。
2. 原有 `OwnedProcess` 先登记宿主与非 root Linux 根，释放外层门；bridge 报告实际出生身份，经独立核验和持久登记后，平台才释放初始化。
3. bridge 在同一原生范围内执行固定初始化函数，继续检查原生路径、0700/0600、UID、软链接、已有所有者和配置字节。函数没有另起无归属进程。15 秒准备期限覆盖这一步，取消、超时和 owner 退出使用同一个范围停止协议。
4. 初始化完成只返回 prepared，不立即启动 CLI。平台核验实际配置 SHA、程序和通信代码，重新检查用户、算例、凭据版本、启动请求及 CLI owner/租约和两级进程绑定；通过后另发 launch，才创建 CLI 子进程并初始化 RPC。

准备中取消不等于消息中断：此时 CLI 尚未启动，也没有模型请求。根或 bridge 的登记提交失败时，初始化函数本身不能执行。准备完成后撤权或替换执行归属，也不能越过最后的 CLI 启动门。

初始化及 CLI 共用 `NativeCliSession` 的持久进程记录和停止回执，没有新增数据库表或迁移。新 WSL profile 增加 `native_state` 和 `transport.native_state_sha256`；旧 profile 形状仍可解码历史。Windows 会话保持原流程。冻结配置现在按真实 UTF-8 字节复核，不能用换行归一化掩盖文件变化。

若 owner 已被替换，旧执行者只能关闭自己持有的进程，不能替新 owner 写停止回执；上层保留 recovery_required。API 猝死测试只证明实际原生范围已退出，不把缺失的持久终态补成成功。初始化中断留下的私有目录也不自动删除或接管：共享账户状态的保留、修复及回收继续属于 N5。

## 验证与证据

最终规范化证据见 `tmp/mainline-N4-cli-preparation-evidence/`，原始测试来源和修正记录见 `evidence-origin.json`。

| 验证 | 覆盖范围 |
|---|---|
| WSL 初始化专项 | 9/9：两级登记提交失败、正常初始化、初始化中取消/超时/失败、准备后撤权/owner 替换、实际 API owner 猝死；模拟 CLI，真实 WSL 与进程范围 |
| WSL 会话双库 | SQLite/PostgreSQL 各 16 项，覆盖持久身份、账户目录、失败清理、配置/发行版撤销、跨用户访问；临时 PostgreSQL 库回收 |
| 实际 Linux Codex | 新启动入口 10 项；六个幂等启动请求、线程恢复、审批、真实 shell、失败/取消与原生退出，本地 Responses fixture，无远端推理 |
| 目录/传输/sandbox | 原生目录权限与软链接 13 项、WSL 传输 7 项、实际文件 sandbox；不声称已验证网络隔离 |
| 宿主兼容 | 5/5：stdio、所属 stdio、持久会话、高层启动请求、S3 会话暂存 |
| profile 历史与拒绝 | 7 项：新记录与旧历史形状可解码；跨用户、配置/路径变化、缺失 helper SHA、错误通信摘要被拒绝 |

故障专项只修改私有测试目录中的 bridge/initializer 副本来制造等待和故障，生产登记与停止代码保持真实执行。测试在初始化阶段读取持久根/bridge 记录，停止后重新探测两者的 Linux 出生身份；无关原生哨兵继续存活。两级登记失败还检查 initializer 入口标记从未出现。

复验命令（全部使用隔离数据库、存储和私有 Linux 目录）：

```powershell
platform/server/.venv/Scripts/python.exe -X utf8 -B platform/server/codex_wsl_preparation_smoke.py
platform/server/.venv/Scripts/python.exe -X utf8 -B platform/server/codex_wsl_session_smoke.py --postgres
platform/server/.venv/Scripts/python.exe -X utf8 -B platform/server/codex_session_request_wsl_actual_smoke.py
```

源码基线：`platform/backups/mainline-N4-cli-preparation-20261103.zip`，实际 SHA 和逐文件匹配见同名摘要及规范化证据目录。head 仍为 `3d1c98e2b754`、35 张业务表；未迁移开发数据库、未重启端口 8000、未改动 UI，未在恢复目录另行重跑。

N4-03 仍需其余专业工具与几何/编译器库内部派生的故障矩阵、全部进程入口可达性复核；只读身份探测属于监督控制过程，须单独核查其期限与清理，不把它当作用户计算载荷。完整 N4/N5、正式 SIWC 部署与 53/295 的既定范围继续保留。
