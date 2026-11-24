# 算例 CLI 契约切换与备份恢复

2026-11-17。设置页新增“CLI 契约与备份”，支持 Claude、Codex、OpenCode、Kimi。选择算例和工具，填写补充工作约定，再保存；平台追加自身管理的契约段，并保留说明文件里原有的用户文字。切换时只移除旧的管理段，不清空其他说明。

Claude使用算例根的`CLAUDE.md`，其他三个工具使用`AGENTS.md`。这些位置分别依据[Claude项目说明](https://code.claude.com/docs/en/memory)、[Codex项目说明](https://learn.chatgpt.com/docs/agent-configuration/agents-md)、[OpenCode规则](https://opencode.ai/v1/docs/rules/)和[Kimi指令文件](https://www.kimi.com/code/docs/en/kimi-code-cli/customization/agents)。具体CLI版本、父目录说明、覆盖文件和自定义agent模板仍会影响最终加载；选择工具并不意味着另外的CLI被禁止读取同一个AGENTS.md。

新会话才会读取新约定。模型凭据、提供方、模型名称和原生协议配置不在本次契约切换中修改；它们仍属于FF-F57/J25尚未完成的完整配置范围。契约是给模型的项目说明，不是操作系统权限控制。

## 操作

1. 在项目栏选择算例，打开设置页底部“CLI 契约与备份”。
2. 选择CLI，填写工作约定，点击“保存契约并备份”。请勿把模型密钥写进说明文件。
3. 点击“前往终端”，新建对应CLI会话。运行中的会话不会自动改配置。
4. 需要恢复时，选择操作前的备份并点击“恢复所选备份”。恢复会先备份当前内容，因此可以再次恢复到恢复前的版本。
5. 若提示配置已变化，先重新读取再决定；不会拿旧页面的内容静默覆盖新修改。

每份备份包含当时AGENTS.md、CLAUDE.md的完整内容和文件原本不存在的状态，以及当时选择的工具、补充约定。支持逐字节恢复UTF-8文件，保留CRLF。最近32次操作前的备份随算例保存在`.vibeflow/cli-contracts.json`；单个说明文件最多32KiB，补充约定最多8000字符。更长期留存应包含在算例备份中。

管理段标记损坏时，重新应用会拒绝覆盖；可以修正文件或恢复已保存的备份。备份本身结构损坏会报错，不自动重建历史。恢复缺失的备份返回404。

## 接口和存储

| 接口 | 行为 |
|---|---|
| `GET /api/v1/cases/{id}/cli-contract` | 返回当前选择、补充约定、文件状态、版本摘要和备份目录；不返回所有历史文件正文 |
| `POST /api/v1/cases/{id}/cli-contract` | 提交harness、instructions、expected_revision，备份后应用契约 |
| `POST /api/v1/cases/{id}/cli-contract/backups/{backup_id}:restore` | 提交expected_revision，备份当前内容后恢复所选快照 |

读取和写入复用算例访问检查；写入复用存储额度、算例协调锁、文件摘要前置条件及既有文件事务。两个说明文件与状态快照在同一文件事务中提交；恢复异常沿现有文件事务恢复入口处理，不另外增加一套恢复机制。

旧`POST /runtime/opencode:apply-prompts`返回410，提示改用算例契约。它以前允许登录用户改服务账户的全局说明，不适合托管多用户；新界面已替换这个入口。原全局文件和备份均未改写，旧状态查询暂保留兼容。管理员如需调整旧全局说明，应在部署维护中自行选择，不自动删除。

## 验收范围

显式真实模型入口为`server/live_llm_cli_launcher_acceptance.py --contracts`，浏览器驱动为`tools/live-cli-launcher-browser.cjs`；执行前核验源码审查摘要。隔离API的四种文件切换、原文恢复、过期提交与其他账户拒绝使用实际数据库、文件事务和HTTP。浏览器保存Kimi约定、切到Claude、恢复Kimi备份后启动真实Kimi和DeepSeek；随机验证标签只写入项目约定，不放进模型提示词。

测试主agent只开放真实Read工具，并通过Kimi文档的`${agents_md}`模板变量载入项目说明；这不替代所有CLI的默认模板、模型提供方或深度协议验收。S3、跨主机和剩余多CLI启动/取消/恢复矩阵仍按原计划，不能因本机契约流程通过就整项关闭FF-F57/J25。

## 本轮结果

最终目录`tmp/live-cli-contract-4skc9etq`的`launcher-result.json`整轮passed=true。真实数据库/文件/HTTP完成四种契约应用、原文件字节恢复、旧版本拒绝、缺失备份404、其他账户读写拒绝与旧全局写入口410。真实Chromium完成保存Kimi、切换Claude、恢复Kimi、启动Kimi、关闭终端、恢复原始说明文件；桌面1600×1000和手机宽度390×844已检查。真实deepseek-flash两次HTTP200，CLI记录1次超时重试；文件随机值与项目约定随机标签都出现在真实回复中。模拟0，未采集token用量。

原失败目录`tmp/live-cli-contract-6unr0q10`保留passed=false，模型0。先后发现select和已填写textarea的嵌套label名称包含控件内容，补充明确aria-label后解决。第三次浏览器操作完成文件恢复后，终端启动受SQLite锁等待影响；当时界面仍在等待恢复请求结束，不能据文件已更新就认为操作完成。

新增接口随后显式结束读取和请求事务，将快照读取、额度检查和最终写入分开；最终写入仍在协调锁内重新核对所有文件摘要，不降低冲突检查。浏览器也等待实际恢复成功反馈后才启动终端。独立新环境最终完整通过，失败轮未改写；不把这次修正扩大为所有数据库并发组合已覆盖。

最终审查为`tmp/N7-05-cli-contract-review-v4-20261117.json`，654个来源摘要。前端构建通过，仍有已有的大chunk提示；impeccable机械检测无问题。首次PowerShell重定向把构建warning记成NativeCommandError，随后使用cmd获得真实构建exit0，原日志保留。

主线48/53，剩5项；N7-05原3组条件继续。全功能295项目标保留，严格整项关闭0、待整项关闭295，已有实现和分项证据不等于0。下一步继续模型提供方/凭据配置切换、各CLI实际启动与恢复，以及剩余部署条件。
## 2026-11-21 CLI 约定读取与备份暂存

查询、应用和恢复在 SQL 写事务外分段读取备份正文。查询返回前仅复核文件版本和访问权限。应用和恢复沿用三文件的原始摘要，文件系统按目标卷预留并暂存，认证 S3 使用持久算例预约与事务日志。提交核对版本，超时等待在途读取结束后返回 504，未提交的请求可按原版本重试。新增备份在落盘前检查已有的 3 MiB 读取上限，超限时保留现有备份。

SQLite、PostgreSQL 分别搭配文件系统和认证 SeaweedFS。四组实际读取 2,054,690 B 的旧备份，每组在首个 64 KiB 后暂停三次。12 次均可取得独立 SQL 写入或算例锁，且同账户另一算例实际保存成功。并发改写后的旧查询与过期应用返回 409，恢复保留原 CRLF 字节，超时未提交且按原版本重试成功。S3 两个首轮的隔离四秒预算也作用于对照保存，纠正夹具后复跑通过。真实 DeepSeek 8 次 HTTP 200，模拟 0 次。临时 API、认证 S3 和 PostgreSQL 已清理。[验收索引](../../tmp/cli-contract-capacity-validation-20261121.json)

