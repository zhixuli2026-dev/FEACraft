# Kimi CLI 本机部署与真实模型验收

2026-11-17。复用现有 Windows/Node/Git Bash，安装官方 npm 包 `@moonshot-ai/kimi-code@2.6.2`，不增加容器或修改系统权限。安装日志为 `tmp/kimi-install-20261116.log`。当前 Node 为24.15.0，Kimi版本命令和平台能力探测均已通过。

## 安装与配置

Windows 使用 `npm.cmd install -g @moonshot-ai/kimi-code@2.6.2 --no-audit --no-fund`。当前命令位置为 `%APPDATA%\npm\kimi.cmd`。平台通过 PATH 或 `VIBEFLOW_KIMI` / `VIBEFLOW_KIMI_DIR` 发现工具；发现路径本身不代表全部会话功能已经验收。安装要求见 [Kimi 官方说明](https://www.kimi.com/code/docs/en/kimi-code-cli/guides/getting-started)。

本次使用独立 `KIMI_CODE_HOME`，模型配置由进程环境传入：`KIMI_MODEL_PROVIDER_TYPE=openai`、`KIMI_MODEL_NAME=deepseek-flash`、`KIMI_MODEL_BASE_URL` 指向已授权的 DeepSeek 服务，`KIMI_MODEL_API_KEY` 仅从本地已有凭据在内存中传递。密钥不写入命令行、算例或文档；验收结束扫描独立 CLI 状态，未发现密钥落盘。未改用户原有 Kimi 配置。

验收专用主配置仅开放真实 Read 工具；随机标识只存在于算例文件，未提供在提示词中。Node fetch 观察器转发真实请求和响应，仅记录目标、HTTP状态、耗时；不生成或替换模型输出。对应入口为 `server/live_llm_kimi_deployment_acceptance.py`，执行前核验648个审查来源与已安装 Kimi 主入口摘要。

## 完整结果

最终证据为 `tmp/live-kimi-deployment-yb8_4s78/kimi-result.json`，整轮 passed=true、进程退出0。真实平台 WebSocket 终端启动已安装的 Kimi，DeepSeek 自主调用一次 Read，工具结果与最终回复均包含文件随机标识；源文件不变。CLI退出0，正式终端关闭接口返回成功，持久会话记录的 cleanup_verified=true，API实际进程已退出。

最终轮记录到 `api.deepseek.com/chat/completions` 两次HTTP200，另有两次CLI超时重试。HTTP计数仅代表已观测到响应的请求，不冒充全部网络尝试次数；观察器没有读取usage，故不报告token总量。全部模拟LLM调用为0。

先前失败保留，不改写为通过：

| 证据目录（tmp/ 下） | 结果 |
|---|---|
| live-kimi-deployment-m3ppyj8_ | 终端等待超时，模型响应0；原入口没有保存终端尾部 |
| live-kimi-deployment-ljlv4dhh | 裸批处理名未被当前目录搜索，等待超时，模型响应0；改用显式 `.\kimi-run.cmd`，保留宿主设置 |
| live-kimi-deployment-_lfda3m4 | 实际模型两次HTTP200并返回随机标识；验收错误地从默认会话列表查已关闭会话，导致StopIteration |
| live-kimi-deployment-996iiz42 | 实际模型两次HTTP200、关闭已核验；验收批处理把退出码0与重定向连写成`0>`，退出码文件为空 |

修正的是验收脚本的路径、会话查询和批处理重定向，产品的关闭条件未降低。以上五轮合计观测到六次真实HTTP200。失败轮也保留API退出记录。

## 后续范围

这次解决Kimi缺失和一个完整实际终端模型流程，不代表FF-F54/F57/J25全部完成。多CLI配置切换、原生协议、取消、恢复和托管部署按原目标继续。后续已修复Windows快捷启动使用显式发现路径，并在真实浏览器验证PATH之外的中文/空格安装目录，见[快捷启动验收](cli-quick-launch.md)；该结果不代替全部Linux与多CLI功能。

主线48/53，剩N4-05、N4-07、N7-01、N7-05、N7-06；N7-05保留原3组完整工具链/手册/正式部署验收条件。295项全功能目标不缩减，当前严格整项关闭数仍为0；这不等于已有实现和分项证据为0。后续按完整功能流程收口，不扩展权限或底层故障设计。
