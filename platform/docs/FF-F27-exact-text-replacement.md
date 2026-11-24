# FF-F27 精确文本替换

`propose_text_replacement` 在读取同一个文件版本和计算 SHA-256 时检查匹配。默认原文只能出现一次，`replace_all: true` 按非重叠匹配替换全部。替换文本可为空，换行、Unicode 和 `$&`、`$1` 等字符按原样处理。路径不存在、缺少原文、匹配不唯一、二进制正文以及会超出文件事务大小的展开会在审批前返回工具错误。 [预检和版本绑定](../server/app/services/agent_file_reads.py) · [执行工具](../server/app/services/tools.py)

匹配按读取块计算，支持跨块的 UTF-8 字符、换行和多行原文。小文件继续留存原差异，大文件的预检按相同规则执行。批准后以已绑定的摘要复核文件，再通过原有普通文件或 caseDict 事务发布。外部改动即使保留了待替换的文本，也会使已登记提案失效。浏览器显示原版本已失效，服务端拒绝旧提案并登记 `file_conflict`。 [审批和状态](../server/app/routers/agent.py) · [历史差异](FF-F26-file-proposal-history.md)

## 实际验收

已登记的 DeepSeek `deepseek-flash` 通过两种官方接口生成工具调用。验收使用实际文件系统或认证 SeaweedFS，并通过 Edge 点击审批、拒绝及查看历史。首轮两个接口把用户传入的 CRLF 转成 LF，真实文件预检返回原文缺失，保留原始字节。后续模型多行验收用 LF，另对实际文件及执行函数核对 CRLF 和跨块语义。 [Completions 首轮](../../tmp/live-text-replacement-mr05qngt/live-result.json) · [Messages 首轮](../../tmp/live-text-replacement-9qal7b7j/live-result.json) · [DeepSeek Messages 协议](https://api-docs.deepseek.com/guides/anthropic_api/)

文件边界检查通过超过历史预览上限的实际文件、跨 64 KiB 的 UTF-8 与 CRLF、非重叠匹配和空文本删除。独立的 128 组分块检查与 Python 字符串匹配计数一致，另核对精确 CRLF 输出、三种错误参数和展开后的大小上限。上述两组未调用模型。 [文件边界](../../tmp/agent-read-boundaries-esk3b5wb/result.json) · [匹配边界](../../tmp/replacement-native-boundaries-42wt7u7e/result.json)

正式矩阵覆盖单处多行、手动全部替换、自动全部替换、删除、拒绝、外部编辑、caseDict、四类无效来源和导入后的只读历史。普通文件和 caseDict 的提交前后实际 API 退出通过独立回归检查。 [真实验收入口](../server/live_llm_text_replacement_acceptance.py) · [实际退出入口](../server/live_llm_agent_dictionary_recovery_acceptance.py)

本轮两个普通文件恢复组各进行了两次实际退出。提交前退出恢复原字节，工具记录为中断。提交后退出保留新字节及唯一工具结果。两个恢复组各在退出前用实际 DeepSeek 生成提案，合计四次真实模型请求。caseDict 的对应 SQLite 与认证 S3 恢复证据来自同一个精确替换工具。 [本轮 SQLite 恢复](../../tmp/live-agent-file-recovery-y1cnz8lr/live-result.json) · [本轮 S3 恢复](../../tmp/live-agent-file-recovery-4lkj4yu2/live-result.json) · [caseDict SQLite 恢复](../../tmp/live-agent-dictionary-recovery-7a0kcqos/live-result.json) · [caseDict S3 恢复](../../tmp/live-agent-dictionary-recovery-obdraow7/live-result.json)

### 2026-11-21 矩阵结果

四种组合各通过 11 个场景及一次只读归档，共 44 个场景和四次归档。四组均检查其他账户审批返回 404、重复审批返回 409，以及文件被改动后直接调用审批只能得到 `file_conflict`。Edge 的原差异回读保留 `$&`、`$1` 和实际替换行，当前文件再次编辑后仍可用。最终页面将失效审批按钮显示为禁用，TypeScript、Vite 生产构建和样式检查通过。Vite 仍报告有分块超过 500 kB。 [禁用状态](../../tmp/live-text-replacement-23x3jxl0/stale-approval.png) · [归档差异](../../tmp/live-text-replacement-rg5gkawl/imported-history.png)

整阶段记录 116 次 DeepSeek HTTP 200，另有一次连接中断，模拟调用 0 次。计数包含初轮换行不一致、测试请求上限、浏览器后台读写与夹具写入竞争、模型参数不一致以及最终复跑。退出验收后的 25 个隔离 API 端口均已关闭。 [逐场景汇总](../../tmp/FF-F27-exact-text-replacement-20261121/validation.json)
