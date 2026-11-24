# N6-07真实Agent、服务重启与提交权限增量

2026-11-12。主线仍44/53，剩9项；295项全功能原目标不变。本记录补充原[浏览器生命周期进展](N6-07-browser-lifecycle-progress.md)，不宣称整项完成。

## 真实模型与文件写入

`tmp/case-workflow-controls-3yf5indg`通过实际375px页面调用已授权DeepSeek配置（来源profile26，隔离profile2，官方api.deepseek.com，deepseek-flash）。模型自主选择`propose_file_update`，审批前目标文件404，页面批准后200，写入恰好一行及换行。实际后续模型回复完成；持久工具记录为executed。冷刷新恢复原会话2和工具结果，没有新POST或新模型调用。文件SHA-256为`94eea0a20f2407e06fd7ad2b0a4b796837e44c3f9e1c9b1a75af77ac06183316`。

该宿主实际2次模型HTTP200，分别5174和5412 tokens。完整证据为其`upstream.jsonl`、`real-agent-before-approval.json`、`real-agent-followup-result.json`、`real-agent-cold-refresh-result.json`及`controls-result.json`。API/preview退出、原流程/步骤/作业/尝试未改均已核验。

## 实际服务进程重启

新增显式入口`live_llm_service_restart_acceptance.py`和测试宿主可选`--restart-on-request`。它在隔离目录中保留同一数据库、文件、索引、加密vault、端口和内存环境；观察到原进程确切退出后启动新进程。没有用复制旧数据库冒充同实例重启；源宿主复制只用于准备隔离环境，随后在新环境内执行真正重启。审核绑定993份源码/构建文件，未运行历史模拟模型集合。

宿主`tmp/case-workflow-controls-ye4hw6k7`原API PID20388确切退出，新API PID25856启动。原浏览器登录仍被接受；冷刷新后会话2、原文件、几何发布、原流程详情的响应字节完全一致，无POST重放。之后在原会话发起新的真实DeepSeek请求，模型实际选择`read_text_file`，读取原文件并在最终回复返回同一行。重启后2次HTTP200，分别5034和5122 tokens；此前没有本宿主模型请求。不存在用旧回复满足新调用验收。

页面在重启后实际下载原STEP/STL，15363/684字节，SHA-256仍为原发布清单中的`da1bb9de000b7ea89cae1aed2b8edee0e68a17442337afeca67335fb4a266d56`和`af97931de52adeeefe0f6ad2c979195559e65c6aec64cc9935a441d8b2786b66`，没有重新导出。宿主最终退出0、旧业务历史未改。证据为`api-restart-ready.json`、`browser-before-restart.json`、`browser-after-restart.json`、`restart-agent-and-download-result.json`及`restart-download-byte-verification.json`。

此项明确覆盖空闲服务重启和后续实际调用，不代替运行中Agent/作业被中断的恢复。两轮真实模型合计4次、20742 tokens、模拟LLM0。后面的两个权限修复没有改动模型服务；上述模型验收发生在权限修改前，不能写成最终源码全部路径已重跑。

## 真实权限反例与修复

基线宿主`tmp/case-workflow-controls-fbl0qh9t`复现两项产品失败，原始结果保留：

- viewer能列出几何发布及下载文件清单，但scene/validation读取误用写权限，返回404“算例不存在”。
- 组织成员为viewer、工作区分享为member时，工作区接口错误返回member，页面允许核对提交；真实写入接口仍404，没有越权执行。

修复只改后端两处：`geometry._document`使用只读授权；`sharing.access_role`返回组织角色与分享角色的实际交集，组织viewer始终限制为viewer。几何修改入口和所有提交的写入门槛保持有效。

最终宿主`tmp/case-workflow-controls-t79k57e1`实际登录原测试viewer，member分享下返回viewer，页面“核对提交”禁用；scene、validation、results均200且指向原几何发布。viewer向单作业、批量、工作流的真实提交均404。

再通过真实管理接口授予member，页面核对单作业后撤销成员资格。点击确认收到404，数据库作业数仍26，原幂等键没有接纳记录；页面显示权限错误并保留原请求。恢复member后实际点击“重试原提交”，沿用原键`b1c132b4-357c-4978-8046-5bb8e5655ed3`及相同正文，只产生作业27。真实Windows命令`echo N6_REAL_PERMISSION_RETRY`成功、日志有原输出、进程停止verified=true；同账户只读回执返回27，另一账户同键found=false，浏览器待确认记录清除。最后恢复原viewer角色和禁用分享，scene/validation/results/artifact读取均404。

权限组真实模型0、模拟LLM0，是实际账户/API/浏览器/数据库/原生命令验收。没有伪造权限响应、作业状态或停止证明。独立核对四宿主均已退出，旧workflow/step/job/attempt完整行未变，新增32个原生请求的32个登记子执行停止均核验，未发现未收集异常标记。详见`tmp/N6-agent-restart-permission-independent-20261112.json`；权限基线的产品失败仍为失败，宿主清理通过不能替代产品验收。

## 下一步和剩余数

继续运行中任务的服务中断与页面恢复，随后按原N6-07的启动、确认、取消、重试、重启恢复、查看/下载逐项退出审计，核对原四条remaining的修复证据。入口合并和完整页面验收组应更新为in_progress；没有据本增量把整项标为complete。主线44完成/3进行中/6待开始，剩9；全功能295项仍未最终整项验收，107待完整回归、89待实施、99其他状态。原主线2～3周低置信度估计未改变，未自动顺延。
