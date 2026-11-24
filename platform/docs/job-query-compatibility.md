# 旧任务查询兼容与运行信息

2026-11-04，N1-07按原条件完成：旧前端继续读取旧终态；新前端显示run/step/attempt/phase；所有查询保留账户归属校验。

`GET /cases/{case_id}/jobs`、单个Job详情和批次成员共用查询视图。原status、exit_code、ended_at、log_path等字段保持语义；新增可空run_id、step_id、step_key和简要attempt。列表通过一次最新attempt查询和一次工作流关联查询读取持久信息，不依赖本API是否持有执行进程。详情补充原有进程、停止、登记和恢复信息。工作流链接只有在run属于该Job的case时才公开。

没有attempt的旧记录返回attempt=null、phase=null，不推断流程、不伪造执行尝试；旧终态、缺失时间和缺失日志保持原行为。历史日志继续按Job保存的log_path读取，SSE结束状态保持与查询一致。前端残差解析也优先使用保存的日志路径。

作业面板默认显示阶段、尝试次数和恢复提示；可展开查看完整运行/步骤/尝试标识及重试来源，避免占满日志区域。历史记录明确标注没有执行元数据。选择任务支持键盘；详情刷新失败和日志中断有文字反馈。切换算例后丢弃上一算例的列表响应。

验证：

- `server/job_query_compat_smoke.py --postgres`：SQLite/PostgreSQL各8项；四种旧终态的详情/列表/批次/增量日志/SSE、缺失日志时间、无本地进程的持久关联、损坏跨case链接和未授权查询。最终结果在`tmp/job-query-compat-evidence-9kkw0di8/result.json`。
- `tools/playwright-job-query.js`：隔离API与真实浏览器7项；使用`tools/job_query_ui_server.py`启动夹具。结果`tmp/legacy-query-browser-result.json`，日志不可用分支通过HTTP故障注入验证。
- `npm run build`通过，记录`tmp/legacy-query-frontend-build-verified.log`。原有历史任务/批量执行专项结果随本批证据归档。

本项不代表恢复操作页面、事件跨实例实时订阅、自动重算或文件补偿完成。这些仍在原N5-07、N6、N2/N3任务中；恢复提示不执行接管或重新运行。开发数据库和8000端口未改动。
