# Linux Node/npm 与 CLI 部署

2026-11-18：Linux API/worker 新增 Node/npm、Codex、OpenCode 和 Kimi。三个 CLI 均完成真实 DeepSeek 文件读取回合；平台内 Codex 会话也完成真实读取、持久化和关闭。Claude 按用户要求不安装、不推进、不发起模型验收。

## 固定版本和安装

| 组件 | 实际版本 | 来源与校验 |
|---|---|---|
| Node.js | 24.22.3，Linux x64 | [官方发行目录](https://nodejs.org/dist/v24.22.3/)，SHA-256 `4f2b175a6d53a5c617a80dba2676238a0688cef28c6da4eba35c7d844e66fcfb` |
| npm | 11.19.1 | 随上述官方 Node 包安装 |
| Codex | 0.169.0 | `@openai/codex`，官方 [openai/codex](https://github.com/openai/codex) |
| OpenCode | 2.0.32 | `@opencode/cli`，[官方安装说明](https://opencode.ai/docs/) |
| Kimi Code | 2.6.2 | `@moonshot-ai/kimi-code`，[MoonshotAI/kimi-code](https://github.com/MoonshotAI/kimi-code) |

`infra/cli-toolchain/package.json` 固定顶层版本，`package-lock.json` 固定 64 个依赖记录及 registry URL、SHA-512 integrity。采用 `npm ci`，不在镜像构建时选择 latest。Node 的 glibc Linux 包与 API 基础镜像匹配；没有把前端 Alpine 镜像中的 musl 可执行文件复制进 Debian API 镜像。

从 `platform` 目录执行：

```powershell
python tools/fetch_node.py
docker build -f infra/Dockerfile.node-cli --build-arg API_IMAGE=vibeflow/api:dev -t vibeflow/api:node-cli .
docker compose --env-file infra/.env -f infra/docker-compose.prod.yml -f infra/docker-compose.node-cli.yml --profile distributed up -d
```

先使用当前应用源码构建 `API_IMAGE`；若部署还需要工程工具，应将已经构建的完整工程镜像作为基底。本机实际基底为上一阶段包含 SU2/ParaView/TeX/OpenFOAM 等工具的工程镜像。`VIBEFLOW_NODE_CLI_IMAGE` 和 `VIBEFLOW_API_IMAGE` 可覆盖 Compose 的成品和基底镜像名，默认值及覆盖值已经实际解析验证。

Node 安装包可通过 `fetch_node.py --archive PATH` 复用，始终校验官方摘要；它保存在被排除的 `.build-inputs`，不混入源码发布包。CLI 安装仍需访问固定 npm registry 地址，首次本机安装原生包耗时约 440 秒，后续应用修改复用已安装工具层。实际运行不需要重新下载安装。

## 实际验收

1. Node/npm 真实打包本地模块、安装依赖、生成锁文件、离线 `npm ci` 并执行构建；读取随机二进制数据，经过 gzip 解压、模块 SHA-256 计算和真实 Node 子进程后与原输入核对。另验证实际非零退出码传递。
2. 三个 CLI 实际 `--version` 和 `--help`，平台能力发现以及阶段 V 的 Node/npm 条件通过。
3. 三个隔离 Linux CLI 分别调用授权的 `api.deepseek.com` / `deepseek-flash`，模型自主读取提示词中没有的随机文件内容并正确回答。Codex 使用 Responses，OpenCode/Kimi 使用 Chat Completions。HTTP relay 原样转发真实上游请求和响应，未生成或替换模型内容；分别记录 2、3、2 个 HTTP 200，共 7 个真实上游响应。
4. 部署后的正式 `/codex/sessions`、turn、events、conversation 路由通过真实 Codex 回合：执行 `/bin/bash -c 'cat Ref/proof.txt'`，正确返回 proof，PostgreSQL 保存 assistant 内容，S3 原文件未改变，turn 清理通过，session 显式关闭并具有真实 scope-empty 停止证明。
5. 该托管回合另有 CLI 报告的 usage：17576 input、186 output、17762 total，其中 cached input 16384。它是原生 CLI 报告，不是单独观测的上游 HTTP 请求计数；不能把一个 Agent turn 与一个上游请求混为一谈。
6. API/worker 实际重建、应用模块摘要一致、健康接口通过，原三份 TeX PDF 逐字节保留。数据库、对象存储和私有密钥卷保持。

安装、文件系统和配置检查属于无模型基础设施验收；模型行为仅以第 3、4 项真实调用为证。所有模拟 LLM 调用为 0。

## 真实联调暴露的兼容问题

首次托管会话返回 502。原 stdio 启动逻辑将 bridge 报告的内层 PID 直接用于 API 所在命名空间的 `/proc` 查询，实际错误为 `/proc/3/stat` 不存在。Linux 路径现在复用现有 `terminal_identity_probe.locate`，从已登记的根进程后代中映射出生标识、命名空间和内层 PID；后续放行前重新核对映射。Windows 和 WSL 分支保持原执行方式。

随后 Codex 成功启动，但私有存储测量拒绝其正常生成的 `apply_patch`、`applypatch`、`codex-linux-sandbox` 符号链接。Linux 私有 CLI 统计现在与既有 WSL 统计一致，只按 `lstat` 计链接自身字节，不遍历目标；用户和节点统计使用同一规则。公开算例仍拒绝链接，根目录不允许链接重定向，特殊文件仍拒绝，条目预算仍生效。5 项实际文件系统检查覆盖普通文件、文件/目录/悬空链接、根目录重定向、FIFO 和条目上限。

上述失败记录保留，未降低进程停止或存储统计要求，也未用模拟推理绕过问题。失败会话均实际停止并确认清理；修复后的正式模型回合通过。

## 运行、恢复和范围

当前 S3 API/worker 使用 `vibeflow/api:node-cli-v3-20261118`。镜像本身不含模型密钥；托管 Codex 使用平台中已授权的 provider profile，独立验收的凭据仅通过 stdin 和子进程环境传递，不放入 Docker 命令参数或公共结果文件。DeepSeek 的既有直连排除项保持。

回滚应选择原镜像并只重建 API/worker，保留原 provider vault、数据库、文件与私有 CLI 目录卷。先让当前会话结束并确认清理；不删除数据卷，不凭旧 PID 重启或杀进程。历史成果通过正式接口核对。完整备份恢复仍遵循[部署手册](deployment.md)。

本阶段证明 Linux 工具安装、三 CLI 独立真实推理和正式 Codex full_access 主流程。其他权限变体、多 harness 平台配置/切换/取消/恢复、浏览器快捷入口的 Linux 组合仍按各自原任务验收；不将独立 CLI 通过冒充这些流程完成。`runtime=windows` 是当前 API 的原生宿主分支历史枚举，在 Linux 上实际启动 Linux 程序；命名兼容不能当作 Windows 模型验收。

证据：

- [阶段汇总](../../tmp/mainline-linux-node-cli-evidence/validation.json)
- [Node/npm 实际运行](../../tmp/linux-node-cli-20261118/native-v1-result.json)、[链接统计实际检查](../../tmp/linux-node-cli-20261118/link-inventory-result.json)
- [Codex](../../tmp/live-linux-cli-codex-b87dc6dc30/result.json)、[OpenCode](../../tmp/live-linux-cli-opencode-a57cae8b08/result.json)、[Kimi](../../tmp/live-linux-cli-kimi-783caa5818/result.json) 的独立真实调用
- [正式平台回合](../../tmp/live-linux-managed-codex-b2617d69152a/result.json)、[原生 usage](../../tmp/live-linux-managed-codex-b2617d69152a/turn.json)、[会话关闭证明](../../tmp/live-linux-managed-codex-b2617d69152a/closed-session.json)
- [PID 映射失败](../../tmp/live-linux-managed-codex-dcfeacfbe6e9/startup-diagnosis-v2.txt)、[实际活动缓存链接失败](../../tmp/live-linux-managed-codex-e7e46ee6cbfb/live-storage-diagnosis.jsonl)
- [最终部署与历史成果校验](../../tmp/linux-node-cli-20261118/deployment-result-v3.json)

主线暂为 49/53、剩 4 项，资源预算 11/17、剩 6 项，全功能 2/295、剩 293 项。随后逐项核对原交付条件与已有证据，满足整项条件后再关闭；不会把其他功能的范围附加到已交付子功能上。
