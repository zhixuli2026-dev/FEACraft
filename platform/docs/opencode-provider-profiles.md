# OpenCode 2 使用平台 API 配置

2026-11-17。终端新增独立的“OpenCode 2 API 配置”选择，复用现有加密vault和本人配置。选择后点击 `opencode ▸`，在新Windows终端内运行发现的OpenCode程序，并附加 `--standalone`；切换配置后新建会话，旧会话保留原配置。选择“使用CLI原配置”恢复原快捷启动行为。

在Agent面板管理API配置，设置密钥、默认模型和基础地址；终端可重新读取配置列表。本人配置才可传入交互式shell，共享凭据不导出。项目说明仍由[CLI契约备份](cli-contracts.md)管理；这里不复制明文密钥，也不把终端历史当作密钥备份。

## 版本和配置

本机实测 `@opencode/cli 2.0.32`。该版本使用 `providers/settings/package`，与V1的 `provider/options/npm` 不同；当前平台绑定针对V2，V1仍可使用原配置启动，不能当作已验证的V1配置转换。

根据[官方提供方配置](https://opencode.ai/docs/providers)构建 `vibeflow_api/selected`，`selected.modelID`对应平台默认模型，`env`引用 `VIBEFLOW_OPENCODE_API_KEY`。OpenAI兼容服务使用 `@opencode/ai/providers/openai-compatible`；Messages服务映射至Anthropic包，但本轮只验收了DeepSeek的OpenAI兼容协议。配置通过 `OPENCODE_CONFIG_CONTENT` 提供，密钥独立留在进程环境中。

使用[2.0.32 standalone实现](https://github.com/anomalyco/opencode/blob/v2.0.32/packages/cli/src/services/standalone.ts)的私有服务器模式；它继承当前CLI环境，客户端退出时结束服务器租约。平台继续按原生进程范围核验关闭，不以CLI单个进程退出替代。无需改服务账户已有的OpenCode后台服务。

状态保存在已有私有CLI根的 `users/<user>/windows/opencode/providers/<profile>/<revision>`，其下独立设置XDG配置、数据、缓存、状态和临时目录。切回同一配置可再次访问其历史；密钥版本变化使用新目录。配置和状态未写入算例或服务账户的个人目录。环境清除继承的OpenCode配置和模拟开关，再设置本次绑定；模型密钥不出现在命令、浏览器或NativeSession配置记录中。项目或系统管理配置仍按OpenCode自身优先级工作，此入口不宣称操作系统隔离。

新建 `/ws/terminal` 使用 `shell=cmd&cli_harness=opencode&provider_profile_id=<id>`。不支持的WSL绑定400，引用他人配置403；已有session不能更换绑定。列表/详情的无密钥 `credential.harness` 区分Kimi/OpenCode。Kimi原有绑定路径保持兼容。

## 验收及未解决问题

入口 `server/live_llm_opencode_provider_acceptance.py`，浏览器驱动 `tools/live-cli-launcher-browser.cjs`，最终审查 `tmp/N7-05-opencode-provider-review-v3-20261117.json`，656个来源摘要，并固定实际OpenCode二进制摘要。

测试使用真实隔离API/vault、文件和终端，真实Chromium在A→B→A三份新终端间切换。两份配置指向同一授权DeepSeek模型，通过本地观测转发器原样转发请求和响应到 `api.deepseek.com`；转发器不生成、修改模型答案。实际read工具读取仅存在文件中的随机值，然后模型回复该值。测试primary agent仅允许read，没有模拟LLM或子代理。

| 轮次 | 实际结果 | 真实HTTP200 |
|---|---|---:|
| `tmp/live-opencode-provider-m5wpttaq` | 推理成功，SQLite锁等待导致关闭记录未能保存；整轮失败 | 2 |
| `tmp/live-opencode-provider-bpkfzq6n` | 同类关闭失败，保存协程等待链诊断；整轮失败 | 2 |
| `tmp/live-opencode-provider-19t3v0hk` | A→B→A、read/随机值回复、3份停止记录及进程退出、私有状态无明文密钥全部通过 | 6 |

全部轮次合计10次真实HTTP200，模拟0；没有完整统计token或CLI内部重试。失败轮记录保持失败，未用成功轮覆盖。

**SQLite间歇性关闭问题尚未定位或修复。** 第三轮仅增强了只读诊断，没有修改数据库行为，因此通过不证明该问题消失。前两轮可见多个API/原生探测任务等待数据库，终端实际停止后完成记录仍待保存。下一步按N7-01原并发/恢复范围定位持锁事务，验证既有“重试保存关闭结果”流程；不新增阶段退出条件，也不降低停止证明。

前端构建通过；Impeccable检测无发现；实际查看桌面1600×1000和手机宽度390×844截图，无页面水平溢出。最终轮3终端、API、Vite均已核验退出。提供方跨服务/跨模型、Anthropic实际调用、WSL、V1和其余CLI深度协议仍未验收。

主线48/53，剩5个大项；N7-05原3组条件保留。全功能295项仍须按原条件整项验收，严格整项关闭0不表示已有实现或分项证据为0。本次是接入功能和真实成功/失败证据，不整项关闭FF-F57/J25。

## 2026-11-17 并发修复与复验

真实基础设施入口 `server/infrastructure_sqlite_terminal_acceptance.py` 同时请求文件树、索引状态、作业批次、CLI契约、套餐和运行环境接口，并启动、执行和关闭实际Windows终端。不调用模型，不生成任何CLI推理结果。

首次 `tmp/sqlite-terminal-concurrency-6px4zd6u` 复现两个套餐请求同时插入 `plans.code`，其中一个返回500。`routers/org.py` 已改为数据库原子 `ON CONFLICT DO NOTHING` 后读取，不覆盖已有套餐。修复后 `tmp/sqlite-terminal-concurrency-8i87dlcy` 三轮通过：36次并发接口请求均200，3次实际命令与关闭成功。该证据验证SQLite；PostgreSQL分支尚未在本轮执行。

随后 `tmp/live-opencode-provider-jobbuuan` 在真实浏览器完成A→B→A验收：6次DeepSeek HTTP200，3次实际read/随机值回复，3份持久关闭记录、进程退出和私有状态密钥检查通过。运行审查为 `tmp/N7-05-opencode-provider-review-v4-20261117.json`，包含657份来源。所有OpenCode轮次累计16次真实HTTP200，模拟0；历史失败保持失败。

本轮被动记录SQL操作类别、连接/任务标识、等待和事务结束时间，不记录SQL参数或密钥。**套餐重复插入修复不等于终端锁等待根因已修复**：本轮未复现原来的持久关闭失败，原N7-01并发/恢复项继续保留。没有新增权限或隔离设计，没有追加阶段退出条件。汇总证据见 `tmp/sqlite-terminal-concurrency-evidence-20261117/validation.json`；主线48/53及全部295项交付范围不变。
