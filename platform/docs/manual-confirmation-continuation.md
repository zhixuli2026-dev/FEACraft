# 人工文件恢复：历史分页与原确认接续

文件版本界面支持加载更早的人工操作记录和后续准备版本，每页20条。人工记录使用算例内事件序号倒序游标，准备版本沿用已有清单游标；加载更多保留当前选择，刷新从第一页读取。原数组历史API保持兼容。

另一浏览器登录原账户后，可从服务端历史“核对原确认”，查看原动作、来源或目标、原因及确认时的版本和事件序号，再明确采用。采用后先在本机会话存储保存原ticket与请求摘要，用户再点击“继续同一次确认”。无需找回旧浏览器的幂等键，也不会生成新确认。

| API（均在`/api/v1`下） | 行为 |
| --- | --- |
| `GET /cases/{id}/file-recovery/actions-page?limit=20&before_seq=...` | 返回`items`与`next_before_seq`；末页为null |
| `GET /cases/{id}/file-recovery/actions/{ticket}` | 增加原请求、请求摘要及当前账户能否接续 |
| `POST /cases/{id}/file-recovery/actions/{ticket}:continue` | 提供`request_sha256`和`confirmed:true`，沿用持久化原操作 |

接续先检查当前写权限、原确认账户和原请求摘要，再进入原隔离或恢复流程。即使已完成，也先授权再返回回执。未知ticket返回404，摘要不符409，其他有写权限的成员不能代替原确认者。接续不能创建缺失的请求。原写者、原版本与目标冲突限制继续检查。已经完成的恢复只返回原结果，后来的编辑保持。

## 本段实际验收

`tmp/manual-confirmation-1ua0ueve/confirmation-result.json`通过：

- FS/SQLite下22条人工记录和22个准备目录分别按7条分4页读取，无重复；旧数组API结果一致。
- 实际隔离后、完成事件提交前令API退出。新API读取pending原请求，沿原ticket完成一次隔离；只有一次提交事件，原文件和后来编辑保持。
- 实际HTTP检查：摘要不符409、未知ticket404、外来账户404、只读成员404、另一个有写权限的成员403。
- 真实页面将人工记录从20条加载至23条、准备选项从21项加载至23项（含原pending选项）。第一次浏览器实际提交成功后中断202响应；第二个独立浏览器初始无登录令牌和确认记录，正常登录、核对服务端历史并接续，读到完全相同的回执。
- 两个API及预览进程由保留的创建句柄确认退出；两个页面终端具有持久停止证明。没有业务Job或模型回合。

同目录`browser-response-loss.json`、`browser-cross-context.json`保留实际请求与回执；分页与显示观察分别保存在`browser-paging.json`、`browser-visual-metrics.json`。深浅色截图已查看，对比度最低5.99/6.42，控件至少44px、键盘焦点2px。390px下工作台仍将侧栏挤至99px，移动工作台未通过，保留在N6。初次登录定位器误用英文placeholder，修正后复用同一浏览器/API；错误记录另存。

真实模型调用0、模拟LLM调用0；这是实际SQL、文件、HTTP、API退出和浏览器验收。907个源码及构建文件绑定于`tmp/N5-manual-confirmation-reviewed-runtime-v1-20261111.zip`。本浏览器阶段范围为FS/SQLite；后续S3实际API接续、准备/上传分页及caseDict证据见[原条件核对](N5-07-original-closure-audit.md)，PostgreSQL完整部署矩阵仍未由这些结果证明。

另有`tmp/workflow-backlink-be_l8b82/binding-result.json`通过：使用上一轮真实CadQuery证据数据库的独立副本，正常重算和迁移历史可读；原步骤当前尝试已指向其他步骤时，恢复方案和重算均拒绝。所有业务行及原数据库未改变，API创建句柄确认退出。

本段没有关闭N5-07整项。后续已验证caseDict与来源变更、上传分页及S3接续，当前两组Jobs剩余项见[原条件核对](N5-07-original-closure-audit.md)；全部53项主线和295项全功能目标保持。
