# FF-J08 四维额度与套餐到期

配额和设置页使用同一当前生效套餐。订阅到期后，新请求按免费额度判定，历史订阅仍可查询。作业和存储继续使用工作区所有者预算，作业领取与可视化启动分别核对实际占用。已有结果、活动作业的明确取消和安全回收继续可用。 [实际作业和可视化占用](global-execution-slots.md)

托管 Agent 新消息、目标回合、原生 Codex 回合、翻译、会话压缩和注入平台凭据的终端启动按请求者记账。每日 UTC 零点重置。同一用户的入场校验与计数在数据库事务中串行执行。已标记的导入历史不计数，升级前当天原有消息仍计入当日。运行被取消时，已经受理的请求仍保留计数。工具往返和同一次已登记原生请求的重放沿用原入场记录。终端记账是每次带凭据的启动，平台看不到手动 shell 里的独立网络调用。

两个真实 API 分别在 SQLite 和 PostgreSQL 验证最后一个模型名额。套餐到期后，两个同时发出的翻译请求一条实际成功，一条返回 429。对新消息、压缩、目标继续和带配置的终端均验证拒绝。作业、报告构建、可视化和文件写入也按到期后的额度拒绝。原文件字节保持，恢复资源额度后实际 Windows 作业输出 `QUOTA_RESTORED`。先前扩展测试将存储和模型同时用尽，终端先触发存储限制，经单独恢复存储额度后确认模型限制正确返回。 [SQLite](../../tmp/live-j08-quota-primary-tlbvx9wp/quota-result.json) · [PostgreSQL 最终复验](../../tmp/live-j08-quota-primary-kviz_20o/quota-result.json) · [临时数据库清理](../../tmp/terminal-agent-postgres-ztl1siyf/cleanup.json)

最终 PostgreSQL 验收实际 DeepSeek Chat Completions HTTP 200 共 3 次。SQLite 与两次前序 PostgreSQL 运行各 3 次，独立基线回归另有 4 次推理 HTTP 200 和 1 次模型发现 HTTP 200。原生 Windows Codex 跨实例停止复验收到 1 次真实 Responses HTTP 200，并在数据库核对 `native=1` 和进程范围为空。合计 17 次真实推理，模拟 0 次。 [真实基线](../../tmp/live-deepseek-sflutd35/live-result.json) · [原生回执](../../tmp/live-f34-native-owner-rjh6l5zx/native-peer-stop-result.json)

Edge 在 1440 px 和 390 px 显示生效的免费套餐、四行预算、今日请求计数及本地重置时间。配额组件无横向溢出，模型调用 0 次。源代码原有大静态分块警告仍在。构建前后 Docker 缓存均为 2.194 GB，11 个运行服务仍在线。C 盘检查分别为 4,030,304,256 B 和 4,030,201,856 B。 [浏览器回执](../../tmp/ff-j08-quota-view-uyg7uas0/browser-result.json) · [窄屏](../../tmp/ff-j08-quota-view-uyg7uas0/quota-390.png)

部署新版 API 和 worker 前迁移到 `2c89ba41f70e`。已对旧版本 SQLite 执行实际前向迁移，既有用户保留且完整性检查通过。PostgreSQL 验收使用完成迁移的临时数据库，运行后已删除。本机既有部署由原服务继续提供。 [SQLite 迁移](../../tmp/ff-j08-migration-887d3rtg/upgrade-result.json)
