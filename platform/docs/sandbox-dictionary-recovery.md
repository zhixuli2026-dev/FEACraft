# 沙箱项目配置恢复

2026-11-16。N4-05 / RB-12、FF-F58、FF-J16 的增量；主线 48/53、剩 5 项，资源清单 11/17、剩 6 块。全部 295 项交付目标继续，不以本次增量关闭完整恢复任务。

终端“恢复文件”中的恢复方式新增“恢复项目配置 caseDict”。选中快照的 `0-caseDict/caseDict` 后，先比较可信初始配置、快照和当前配置，再显示当前版本与阶段。明确勾选后才能提交。初始配置与沙箱相同则保留当前内容；当前已等于目标则不再增加版本；双方修改不同则保留冲突。仅版本号变化、内容未变也会使预览失效，需重新预览。

此流程复用项目配置编辑器的语法、结构和阶段校验，以及 `write_case_dict` 的文件事务。配置、case-events.jsonl、provenance.jsonl 与数据库版本在同一提交中更新，来源为 `sandbox-recovery`。恢复不绕过阶段推进确认。确认请求沿用持久 UUID 和回执查询；重复确认返回原回执，不覆盖恢复后的新编辑。

API 沿用 `restore-plan`、`restore` 和 `restores/{request_id}`，新增 `semantics: case_dictionary`，只接受一个规范路径 `0-caseDict/caseDict`。计划摘要绑定配置版本、当前阶段与三方文件摘要。旧快照缺少可信基线时不支持本方式；普通文件写回和批量文件改动恢复仍拒绝 caseDict。快照中配置被删除或变为目录时，请下载并在配置编辑器核对，不能借本接口删除配置。

## 实际验收

显式入口 `platform/server/live_llm_sandbox_dictionary_acceptance.py`。执行前审查 `tmp/N4-05-sandbox-dictionary-review-v1-20261116.json` 中 581 个源码及传递辅助文件。沿用真实 WSL 沙箱、API 实际退出、恢复快照和旧会话释放流程；在沙箱内部生成随机内容并追加到实际 caseDict，不把随机答案写入模型提示词。

- FS / SQLite：`tmp/sandbox-recovery-ui-retirement-k1eqiypw/dictionary-result.json`。普通文件入口保护、当前阶段变化拒绝恢复、同内容新版本使预览失效、并发内容冲突、正常配置与两份历史共同提交、重复确认保留后续编辑通过。第二个日志文件实际应用后退出 API，重启后实际配置、两份历史和 SQL 版本全部回滚；同一确认随后成功。
- 实际 SeaweedFS / SQLite：`tmp/sandbox-recovery-ui-retirement-mkhp6vsd/dictionary-result.json`。同样的阶段/版本/三方冲突、提交与重复确认通过。本轮没有注入 S3 写入中途退出，不能以 FS 故障结果代替 S3 故障验收；未在本轮执行 PostgreSQL 组合。
- 每个存储各 2 次真实 DeepSeek HTTP 200，配置及服务返回模型为 `deepseek-flash`。模型自行选择 `read_text_file`，读取恢复后的 caseDict，返回提示词中没有的随机字符串；工具结果和回答持久化，模型读取前后文件内容相同。共 4 次真实请求，模拟 0；详见各目录 `live-result.json`、`live-model-events.json`。
- 实际网页：明确选择与勾选、仅版本变化导致旧预览拒绝、重新预览确认写入、实际内容摘要与快照一致、版本 13 及两份历史一致、关闭重开只读回执而不再 POST。390px 深浅主题和 1280px 桌面截图已目视检查，没有横向溢出或 JavaScript pageerror。见 FS 目录 `browser-finished.json` 和对应截图。
- 前端构建及 Impeccable detector 通过，记录为 `tmp/N4-05-sandbox-dictionary-ui-checks-20261116.json`。原有大 bundle 提示保持。测试 API、网页预览和自建 SeaweedFS 均已停止；FS 的 13 个、S3 的 7 个原进程身份独立核验均已不存在。

准备审查文件时曾误用 Python 编码名 `utf8-sig`，导致审查生成失败，紧接着的入口因缺少审查文件拒绝启动；修正为 `utf-8-sig` 后重新审查，失败期间未启动测试环境、未调用模型。网页初次使用桌面标签和错误的控件名称定位超时，改用实际可见的移动面板选择器；没有强制点击或替代接口伪装网页操作。首次预览截图文件名含 light 但实际为深色，最终结果截图已正确命名并核验。

## 后续原范围

目录创建/删除、文件与目录互换及 Linux 模式位恢复已有后续 FS/S3、网页与真实沙箱证据，见 [目录树恢复](sandbox-tree-recovery.md)。正常导出、其他执行副本中的权限延续及原存储、并发和资源组合继续按原计划验收；不能以这些增量关闭原完整交付目标。
