# FF-F54 CLI 自动发现完成审查

2026-11-18：按原始 F54 条目关闭 CLI 自动发现、显式路径、版本目录、PATH 后缀和 WSL Node 检测。全功能 **4/295，剩 291**；主线 **49/53，剩 4**；资源 **11/17，剩 6**。整个项目仍在实施，当前条目不能替代其他 CLI 会话、沙箱、审批和全局交付任务。

## 原始范围与证据

原始定义来自 `feature-gap-audit.md` F54 和 `full-feature-tasks.json` FF-F54。该条目是发现与能力报告契约；原备注明确把 Codex stdio 握手分配给 F53。按交付计划第 5 节与 T08 核对如下：

| 要求 | 当前实现与实际证据 |
|---|---|
| 显式路径优先 | `VIBEFLOW_<TOOL>` 优先于版本目录和 PATH；实际 Windows Codex 执行 `--version` 成功。 |
| 版本目录与排序 | 实际目录候选指向已安装 Codex；稳定版、RC、beta、alpha 和数字后缀排序通过。测试目录标签仅为排序输入，未冒充安装了不同版本。 |
| PATH 与四种后缀 | `.exe/.cmd/.bat/无后缀` 均发现并执行真实 Codex；批处理只转发到真实程序，不生成固定版本或模型回复。修复 `env` 的 PATH 被忽略的问题。 |
| 错误、恢复和配置边界 | 显式路径不存在时拒绝回退；修复文件后无需重启即可重新发现；空 PATH、显式 `path_dirs` 覆盖正常。环境配置由部署管理员提供，不接受 Web 用户任意执行回调。 |
| WSL Node 检测 | 通过显式执行回调以 `nobody` 运行真实 WSL 命令，UID 65534；发现 Codex 与 Node 并实际执行。实际限制 PATH 后 Node 返回 127，结果正确保留 CLI、将 `node_version` 置空。无回调不隐式启动 WSL，非法回调被拒绝。 |
| 公开能力 API 与兼容 | 部署后的 `/api/v1/runtime/capabilities` 同时保留 `cli_agents` 和 `cli_agent_details`；Codex、OpenCode、Kimi 路径一一对应、环境注册表可用、阶段 V 通过。未登录及无效令牌均为 401。 |
| 真实模型使用发现结果 | 新部署中，正式 Codex 会话通过 Responses 接入授权的 `api.deepseek.com / deepseek-flash`，模型实际执行 `cat Ref/proof.txt`，返回未放入提示的随机内容；PG 对话持久化、S3 原输入保持、回合结束和会话关闭的停止证明均通过。 |
| 部署、数据、回退 | API/worker 均替换为新镜像，全部 app 文件摘要与当前源码一致；健康检查通过，已有三份 TeX PDF 字节摘要保持。前一镜像与私有 Compose 备份保留，可在空闲时恢复。 |

F54 提供后端公开 API，不新增独立 UI 流程。WSL 执行回调仍由所属执行通道显式提供；普通能力路由进行主机文件发现，不私自创建未登记的 WSL 子进程。`runtime: "windows"` 是既有本机执行通道兼容枚举，在 Linux 主机上也沿用；这不是把 Linux 程序报告为 Windows 二进制。

## 具体证据

- `tmp/cli-discovery-actual-_h3yzp1b/result.json`：6 组检查、14 次实际命令，真实模型 0、模拟模型 0。Windows Codex `0.169.0-alpha.11.1`；WSL Codex `0.167.0-alpha.4.1`、Node `24.22.3`。WSL `--version` 的默认 nobody home 别名写入警告完整保留；命令成功，正式会话使用其独立私有 home。
- `tmp/review-cli-discovery-actual-v1-20261118.json`：新入口及其本地导入的 SHA 审查。没有执行旧的模拟发现 smoke，也没有伪造 CLI 推理。
- `tmp/cli-discovery-wsl-install-20261118.json`：复用已校验的官方 Node 包，安装到 `/opt/vibeflow-node-24.22.3`。仅安装步骤使用 root；普通验证用 nobody。
- `tmp/cli-discovery-deploy-20261118/deployment-result.json`：新镜像 `vibeflow/api:cli-discovery-20261118`，API/worker 摘要、健康和三个历史 PDF 哈希。
- `tmp/cli-discovery-deploy-20261118/capabilities-auth-result.json`：两个未授权负例。
- `tmp/live-linux-managed-codex-3c41ee021a3a/`：实际模型回合、事件、能力结果、对话、会话关闭及使用量。case 29、conversation 10、session `a27dfc6f-8b10-4de0-a513-ca165e3b4413`、turn `81efa38a-058b-46fa-820a-da60cec10f03`。CLI 报告总输入 17582、输出 170、总 tokens 17752，其中缓存输入 16512。这是一个正式模型回合；没有把回合数当作上游 HTTP 请求数。
- `tmp/review-cli-discovery-live-20261118.json`：复核此前真实入口及导入辅助文件的哈希，只有发现函数发生变化。仅调用现有 `source(26)` 读取授权配置；密钥不输出。
- `tmp/FF-F54-completion-evidence/validation.json`：完成对账、当前发布归档及校验摘要。

前一轮 WSL PATH 缺失的实际失败保留在 `tmp/cli-discovery-actual-wsl-v2-20261118.json`。另一次把 `--user` 放在 `--exec` 后面的命令构造错误保留于 v1；这两项均未标为成功。

## 使用、安装与回退

Python 默认调用使用进程环境；传入 `env` 时按照该字典的 PATH 查找，传入 `path_dirs` 时按该列表覆盖。完整显式配置字典如 `{"VIBEFLOW_CODEX": ".../codex.exe", "PATH": "..."}`。空 PATH 表示不从主机进程继承搜索目录。

本机 WSL 使用既有独立 Linux Codex：

```text
/usr/local/bin/codex -> /mnt/d/vibeflow_reverse/.tools/codex/0.167.0-alpha.4.1/linux-x64/bin/codex
/usr/local/bin/node  -> /opt/vibeflow-node-24.22.3/bin/node
/usr/local/bin/npm   -> /opt/vibeflow-node-24.22.3/bin/npm
/usr/local/bin/npx   -> /opt/vibeflow-node-24.22.3/bin/npx
```

Node 来自现有 `platform/.build-inputs/node-v24.22.3-linux-x64.tar.xz`，SHA-256 `4f2b175a6d53a5c617a80dba2676238a0688cef28c6da4eba35c7d844e66fcfb`。安装前确认四个链接均不存在；未覆盖已有程序。跨机器部署可用 `tools/fetch_node.py` 准备同一包，再解包到管理员选择的目录。CLI 路径须指向独立固定安装，不依赖编辑器扩展更新。Linux Docker 的完整安装见 [Node/CLI 部署](linux-node-cli-deployment.md)。

需要撤回本次 WSL PATH 增补时，先确认四个链接的目标仍与上述一致，再只撤回这些链接；保留现有 Codex 程序、私有会话目录及数据。容器回退可在没有运行作业/会话时使用 `vibeflow/api:node-cli-v3-20261118`，私有 Compose 修改前备份为同目录 `compose-before-cli-discovery-20261118.json`；其中含部署密钥，不放入发布包。

所有 Claude 接入和模型验收按用户要求停止；保留既有兼容字段不构成继续推进 Claude。全局的沙箱、外部资源、其他 CLI 功能和 295 项最终门槛继续保留，不在此条目中宣告完成。
