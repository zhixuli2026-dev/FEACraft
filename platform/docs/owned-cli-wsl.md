# N4-03.g2b3c：WSL 原生 CLI 与 Windows sandbox 状态

N4-03.h：复现并修复API退出后WSL只读身份探测残留；身份/ancestry改用所属Windows范围、固定Linux读程序、10秒原生期限及管道退出。专项11（取消0.328秒/API猝死0.671秒/期限10.532秒）、登记双库各20、PTY双库各44、CLI/基础兼容5/5通过（实际Linux CLI10、准备9、传输7）。不创建业务Job，不改变原持久登记后放行；53/295、11完成/16实施中/26待实施和head3d1c98e2b754/35表不变。 详见[所属只读身份探测](owned-control-readers.md)。

N4-03.h后续：WSL私有目录初始化已从直接subprocess移入登记后的Linux bridge；准备后重新核验配置、访问权和执行归属，再单独放行CLI。初始化专项9、双库会话各16、实际Linux CLI10、目录13/传输7/文件sandbox、宿主兼容5/5及profile历史/拒绝7项通过。owner变更仅清理自己持有的进程，不伪造持久回执；残留目录/未知结果继续N5。head仍3d1c98e2b754/35业务表，开发库未动，53/295和11完成/16实施中/26待实施不变。 详见[所属CLI初始化](owned-cli-state-preparation.md)。

g2b3f通过Linux原生CLI图片/续接/带图fork与父子独立共5项，使用临时原生目录及Linux本地Responses，见[native-cli-images.md](native-cli-images.md)。正式部署边界仍适用。

当前统一聊天已接入Windows/WSL选择、环境查询、刷新恢复、审批与取消，见[native-chat-ui.md](native-chat-ui.md)。以下保留g2b3c底层运行时历史证据，正式部署边界仍适用。

2026-11-03。沿用已确认的对外托管方案：网站身份采用 Sign in with ChatGPT，模型调用使用独立 ProviderProfile API 凭据。本单元接通 WSL 原生 Codex 的正式会话/回合 API，并增加 Windows sandbox 就绪查询。正式 OpenAI client、公网回调、部署机初始化和完整聊天 UI 仍待后续；不代表完整多租户生产验收。

## 已实现的契约

`POST /api/v1/codex/sessions` 新增 `runtime: "windows" | "wsl"`，默认仍为 Windows。用户不能提交发行版、程序、环境变量或私有目录。WSL 发行版取服务器 Setup 的受限 `wsl_distro`；程序取 `VIBEFLOW_CODEX_WSL_PATH`，或已发现 Windows Codex 安装包旁的 `linux-x86_64/codex`。启动前验证 Linux x86_64 ELF 与文件摘要，直接执行原生程序，无需 Node 或 shell 包装。

部署时应显式配置 `VIBEFLOW_CODEX_WSL_PATH`，指向独立于编辑器扩展目录的固定 Linux 程序。2026-11-15 的实际验收发现：旧扩展被移除后，历史 Linux CLI 随之消失，新 Windows 扩展也未附带该程序，session 创建因而返回 503。不要依赖扩展旁的自动发现作为长期部署配置；安装来源、版本、摘要及后续真实验收见 [CLI 模型连续性](cli-native-model-continuity.md)。

WSL 以 `nobody`（UID 65534）进入既有私有 user/mount/PID namespace。先提交宿主根和 Linux 根出生记录，再核验桥接进程在实际原生根的后代范围内，提交桥接出生记录后才放行 CLI。桥接自身所见的 namespace PID 通过独立 `/proc` 探测映射到 WSL 宿主出生身份，不能直接当作可发送信号的宿主 PID。固定桥、身份模块和原生探测模块摘要进入选择记录；停止仍等待真实范围退出。

会话选择记录包含 runtime、发行版、原生 state root、Windows 可访问的私有 home、Linux argv/cwd 对应关系和代码/配置摘要。读历史验证这些绑定，活跃操作及续租复核发行版和 state root。配置撤销返回 `409 input_stale` 并等待清理；历史读取不重新启动进程。跨运行环境/账户目录/凭据版本不能续接旧线程。新建与恢复线程均使用 Linux cwd，产品 case/S3 协调继续使用宿主路径。

## 原生私有目录与环境

真实 CLI 最初在 `/mnt/d` 的 Windows 目录启动失败：系统调用记录显示 `chmod(..., 0700)` 和 `fchmod(..., 0644)` 返回 EPERM。不能用 Python 可写测试代替 Codex 的文件系统要求，也没有通过改为 root 执行来规避。

新增必填部署配置 `VIBEFLOW_CODEX_WSL_STATE_ROOT`。它必须位于 Linux 原生文件系统，由 UID 65534 拥有且权限为 0700。固定、限时的初始化 helper 只创建该目录内的账户子目录，并验证所有者、权限、真实路径、普通文件和 owner marker。已有 home 缺少 marker、配置改变、符号链接、宽松权限或跨发行版 UNC 映射均拒绝接管。没有配置时，WSL 会话准入明确返回 503。

目录布局为 `STATE_ROOT/users/{uid}/wsl/{distro}/[providers/{id}/{revision}/]home`；同账户同凭据版本可保存线程历史，Windows 与 WSL 状态分开。API 通过对应 `\\wsl.localhost\{distro}\...` 路径校验配置；真实 CLI 只收到 Linux 路径。这个私有目录不进入算例复制、S3 同步和白名单源码快照。Windows 侧仍有私有账户 marker/配置种子，用来保持相同准入契约。

WSLENV 只传输显式选择的目录别名和 API 凭据变量名；目录值在服务器端完成映射。使用别名是因为 `wsl --user` 会覆盖 HOME。固定桥接器随后清空发行版环境，只保留私有 HOME/CODEX_HOME/TMPDIR、固定 PATH/LANG 和当前凭据。凭据只在进程环境中，argv、WSLENV 名称表、配置和产品事件不含凭据值；真实 Linux 工具 shell 已验证不继承它。

这里的账户目录分离不等于独立 OS 用户/容器。多个账户仍使用同一个 worker UID，`full_access` 仍是显式的完整访问模式。进程生命周期隔离、Codex 文件访问策略和多租户安全隔离应分别验收；本单元没有验证全部网络限制、资源限额或敌对租户场景。

## Windows sandbox 查询

`GET /api/v1/codex/sessions/{id}/sandbox` 仅允许实际活跃会话的创建者访问；与其他只读 CLI RPC 共用权限/owner 复核。Windows 调用已安装 app-server 的 `windowsSandbox/readiness`，只接受 `ready`、`notConfigured`、`updateRequired`，未知结构关闭会话并保留停止回执。

返回字段是 `runtime`、`legacy_readiness`、`mxc_availability`、`setup_authority`。Windows 的 MXC 字段固定为 `not_probed`，WSL 两项均为 `not_applicable`，部署权限字段为 `deployment_operator`。WSL 不调用 Windows RPC。该查询没有把旧版 readiness 宣称为 MXC 支持或实际文件/网络隔离证明。

本机 Windows build 19045，实际 CLI 查询为 `notConfigured`；独立 MXC 命令探测退出 1，提示执行器不支持 native MXC，所属范围已核验退出。未执行 elevated/unelevated 初始化，未修改用户、ACL、防火墙或机器策略。Web 组织管理员不等于 Windows 部署管理员，因此没有开放网页直接触发主机 UAC 的接口。

官方 [Windows sandbox 文档](https://learn.chatgpt.com/docs/windows/windows-sandbox)区分 MXC 与旧版实现，并提供单次 MXC 探测。旧版 elevated 初始化涉及低权限用户、文件系统权限和防火墙/策略。部署机需要独立验收该步骤；当前状态查询和底层 setup 客户端不能代替它。

## 部署步骤与剩余验收

1. 在选定 WSL 发行版上保留已验证的非 root worker、Python 3、unshare 和 Linux x86_64 Codex 完整安装包；程序辅助资源与主程序一起保留。显式程序路径只由部署配置提供。
2. 为服务准备新的原生持久目录，例如 `/var/lib/vibeflow-cli`，UID/GID 65534、0700。已有目录应先核对归属，不得直接接管未知数据。设置 `VIBEFLOW_CODEX_WSL_STATE_ROOT`，不要使用测试 `/tmp` 目录或 Windows 挂载目录。正式服务账户还须能读取相应 WSL UNC 路径。
3. 按官方 [Linux sandbox 前置要求](https://learn.chatgpt.com/docs/sandboxing)准备 bubblewrap。本机未安装发行版 bwrap，本次命令使用 CLI 内置 helper，并验证工作区内可写、外部测试目录不可写；没有修改系统 AppArmor 或 user namespace 策略。仍需部署机网络/文件策略矩阵和持续进程验收。
4. 备份数据库并在目标环境审核后迁移到 `1b9a76c0f532`。本次只修改 CLI runtime 约束，业务表仍 33 张；有 WSL 历史时拒绝降回仅 Windows 的约束。开发库没有升级或重载。
5. 将正式网站 SIWC client/callback 与 ProviderProfile 分开配置；用正式产品入口验证会话、批准、取消、退出、预算和重启恢复，再接入完整聊天 UI。

本次测试仅创建并删除自身 Linux `/tmp/vibeflow-cli-fixture-*` 目录，使用临时 API、SQLite/PostgreSQL 和本地模拟提供方。没有初始化正式 WSL state root、修改用户 `.env`、启动付费推理或采用 CLI 浏览器/设备码登录。

## 验收与复现

证据目录：[`tmp/mainline-N4-cli-wsl-evidence`](../../tmp/mainline-N4-cli-wsl-evidence/evidence-origin.json)。

| 验证 | 本批结果 |
|---|---|
| `codex_wsl_transport_smoke.py` | 7 项；非 root、启动门、UTF-8、异常收尾、独立 session 子进程的出生与实际退出 |
| `codex_wsl_state_smoke.py` | 13 项；真实 Linux 权限、符号链接、marker、配置和跨发行版映射 |
| `codex_wsl_session_smoke.py --postgres` | SQLite/PostgreSQL 各 16 项；提交失败不放行、活跃配置撤销、跨用户拒绝 |
| `codex_wsl_actual_smoke.py` | 7 项；真实 Linux CLI、本地 Responses、SSE、resume、用户选择、工具凭据隔离、失败和取消 |
| `codex_wsl_sandbox_smoke.py` | 真实 CLI 命令；工作区内写入成功，外部测试目录写入被拒绝；未验网络 |
| `codex_session_actual_smoke.py [--runtime wsl]` | Windows/WSL 实际 metadata、模型列表、线程和退出；Windows readiness 查询 |
| `codex_session_smoke.py --postgres` | 各 36 项，含状态投影、非法 readiness 与故障回执 |
| `workflow_migration_smoke.py --postgres` | 各 131 项约束、33 表、metadata diff=0、Windows 会话及回合迁移往返、WSL 历史降级保护 |
| 定向兼容 | 7/7：凭据、所属 stdio、sandbox、stdio 协议、回合 API、回合协议、S3 可控暂存 |

在 `platform/server` 使用 `.venv/Scripts/python.exe -X utf8 -B <script>`。WSL 测试需要实际发行版访问；真实 CLI 套件要求完整已安装程序。迁移脚本只创建和清理随机命名的本地测试库。未重跑 core/CAE/离线全集，未做本批 UI 改动；前序网站登录证据仍见 [website-sign-in.md](website-sign-in.md)。

下一单元继续统一聊天 UI 与运行环境选择、部署管理员 sandbox 设置流程、实际 SIWC 公网联调，以及 fork/历史分页、usage/附件/MCP、专业入口、N4 取消/预算/停机与 N5 恢复。53 项主线、295 项功能和现有状态统计保持不变。
