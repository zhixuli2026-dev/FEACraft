# CLI 本地私有状态的节点空间准入

2026-11-07，接续 [CLI S3 准备](cli-staging-lock-deadline.md)。此前节点预算只扫描公共文件根，配置在外部的 CLI 私有账户、会话和图片输入不参与节点总量判断。本次将宿主私有目录纳入既有节点预算，并为平台负责的初始化和图片副本写入预留空间。

## 实现

`cli_private_paths.root` 统一计算私有根，沿用 `VIBEFLOW_CODEX_STATE_ROOT` 或公共根的父目录；拒绝私有根指向公共存储、个人 Codex 状态或通过重定向改变位置。扫描只读取目录/文件元数据，不读取账户配置或认证内容。

每次 `node_storage_budget` 准入现在都在同一进程间 OS 锁内统计公共文件与宿主 `.private-cli`，使用原有扫描期限、条目上限及 `VIBEFLOW_NODE_STORAGE_LIMIT_MB`。因此历史私有文件、tmp、inputs、workspace 也会影响普通节点缓存的准入。无法确认的链接、特殊文件或超过扫描边界的目录明确拒绝，不按零用量处理。

私有写入使用同一预留文件协议；除了检查公共节点卷，还检查实际私有目录所在卷的剩余空间，首次目录尚不存在时检查其最近的已存在父目录。预留按已核验的 API/写入进程出生身份持有；过期时间不用于释放。确认旧进程退出后可以回收它的预留，已经留下的文件仍参与扫描。

账户/会话初始化在写目录前预留 64 KiB，并在写入前核验默认配置、替换配置和 owner 描述的编码总量不超过该界限。预留在实际同步操作完成后释放。此预留只对应平台的初始化写入，后续 CLI 自身产生的文件仍需运行期控制。

图片 publication 在创建目录或写任一副本前，为本地私有输入预留字节；FS 模式还预留公共文件副本，S3 模式的节点预留只覆盖本地私有副本。两份文件依然使用原有公共存储与回滚规则。`codex_turns` 调用图片写入与回滚时改用受保护线程，事件循环不再执行这些文件 I/O；调用者仍等待实际写入/回滚结束，既有 case 锁范围未在本轮改变。

## 实际验证

显式入口：`platform/server/live_llm_cli_private_capacity_acceptance.py --source-profile-id 26 [--postgres] [--s3]`。本轮覆盖 FS/SQLite 与真实 SeaweedFS/PostgreSQL，各 10 项：

| 检查 | 实际操作与判据 |
| --- | --- |
| 保留私有文件计量 | 写入实际 2 MiB 私有文件，将节点限额设为 1 MiB；普通节点分配被拒绝 |
| 初始化准入 | 同样超额时，真实初始化在创建会话目录前被拒绝，已有文件保持原字节 |
| 图片提前拒绝 | 通过真实图片参数验证的 PNG，在私有和公共副本写入前被拒绝 |
| 图片两份副本 | 解除限额后，实际私有文件与 FS/S3 公共文件字节完全一致 |
| 未提交图片回滚 | 实际删除两份未提交副本，保留原有回滚约束 |
| 符号链接 | 创建真实目录链接；准入拒绝，链接外的哨兵内容保持不变 |
| 有界扫描 | 实际目录超过配置条目数时拒绝，不返回零用量 |
| 跨进程预留 | 独立 Python 进程持有实际预留，另一个分配被拒绝；持有者释放后可以分配 |
| 进程退出后的预留 | 独立写入进程实际 `os._exit(77)`，核验其出生身份退出后回收预留，保留它写下的实际文件 |
| 正常 CLI 生命周期 | 已安装的真实 Windows Codex 初始化和关闭，持久停止证明成立，根进程出生身份已退出 |

证据：[FS/SQLite](../../tmp/live-cli-startup-hmn1ij8i/cli-private-checks.json)、[S3/PostgreSQL](../../tmp/live-cli-startup-hd25cmx2/cli-private-checks.json)。共 **20 项实际基础设施检查**，两套最终节点预留目录均无残留。没有调用模拟 provider 或模拟 CLI 推理。

前两次验收脚本失败保留：[图片参数类型错误](../../tmp/live-cli-startup-635imhpb/live-result.json)、[独立进程准备失败](../../tmp/live-cli-startup-06_9itci/live-result.json)。图片参数改用实际 `NativeImageIn`；子进程补上应用模块路径并兼容 Windows 标准输出换行后完成复验。两次失败均未进入模型阶段，真实模型 0、模拟 0，不计通过覆盖。

## 真实模型与收尾

两套实际检查完成后，真实平台 Agent 自主调用 `read_text_file` 读取记录摘要，流式回复与持久回答严格匹配：

- [FS/SQLite 模型证据](../../tmp/live-cli-startup-hmn1ij8i/live-result.json)：2 次真实 DeepSeek，8,742 tokens。
- [S3/PostgreSQL 模型证据](../../tmp/live-cli-startup-hd25cmx2/live-result.json)：2 次真实 DeepSeek，8,562 tokens。

共 **4 次 `api.deepseek.com / deepseek-flash` 调用，全部 HTTP 200，17,304 tokens，模拟 LLM 0**。两项模型读回与基础设施检查分开统计。本次没有发起 CLI 内部模型回合，也没有声称完成含图片的 CLI 模型回合验收。

两个实际 CLI 根均已退出，独立预留进程均已等待至终态；两套 API 已退出。[真实对象存储已停止](../../tmp/s3-attempt-store-bj4x5bco/cli-private-run.json)，[临时 PostgreSQL 已删除](../../tmp/cli-private-postgres-u629z9tx/result.json)，开发数据库未修改。

## 剩余交付

本次接入宿主本地目录的节点级预算；用户配额归属、旧 session 目录的用户绑定和维护、CLI 自行写入的日志/状态/tmp 增长、秘密字段 vault 更新的写入预留、WSL 原生私有根计量与强制限制仍待。扫描与预留协调平台写入，运行期 CLI/外部写入的并发增长需要进一步监测和隔离。保留目录不会因超过 TTL 或失败就自动删除。

图片 publication 的文件副本已实际验证，包含模型选择、图片读取和审批/取消的完整 CLI 回合仍须真实模型联调。S3 公共图片的用户额度、提交/回滚故障、跨卷容量故障和其他节点写入入口的完整资源矩阵继续。

下一步补齐私有账户更新（含 vault）的峰值准入及会话运行期用量观察，再推进 WSL 原生状态、保留目录维护和全局并发。主线完成 33/53、剩 20；资源完成 11/17、剩 6；全部 295 项功能交付目标继续。
