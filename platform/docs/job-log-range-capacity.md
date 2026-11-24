# S3 作业日志分页读取

2026-11-17，N4-05 / RB-14 的具体缺口。主线保持 49/53、剩 4 项；资源清单保持 11/17、剩 6 块。Claude 已按用户要求取消，本项没有模型调用。

## 实际问题与修复

原 `job_log_reader.page` 在 S3 上先下载整份日志，再按 offset/limit 切片。隔离环境运行真实 Windows Python 作业，输出中文及 emoji；正常关闭 API 后重新启动，确认没有本地 JobProcess，强制通过实际 S3 读取。请求 limit=65536 时，后端实际读取 2,300,041 字节，返回 65,535 个完整 UTF-8 字节。原证据 `tmp/job-log-range-capacity-v_r46eqi/range-result.json` 标记 `passed=false`、`reproduced=true`，原 645 份源码保存在同目录 `reviewed-sources.zip`。

修复复用现有 `FileRead`：HEAD 获取长度和对象身份，GET 使用 If-Match 与 Range；校验 Content-Length、Content-Range 和 ETag，finally 关闭实际响应体。空区间不发起正文 GET；读取期间对象变化在普通 GET 返回 409，游标 SSE 沿用现有错误事件。保持本地活跃作业读取、UTF-8 游标、越界重置及未指定 limit 时的完整读取行为。

## 实际验收

入口 `platform/server/job_log_range_capacity_acceptance.py --require-ranges`，最终审查 `tmp/N4-05-job-log-range-review-v5-20261117.json` 覆盖 645 份源码及导入辅助代码。观察器仅包装真实 Client 的 get_object 返回体，统计真实 read/close 调用，不替换字节、网络结果或模型。

最终结果 `tmp/job-log-range-capacity-a1fnpqvh/range-result.json`：

| 场景 | S3 实际正文读取字节 |
| --- | ---: |
| 首个 64 KiB 分页 | 65,536 |
| UTF-8 游标续读 | 65,536 |
| end_offset 限定 100 字节 | 100 |
| 超过文件末尾后重置 | 65,536 |
| 正好位于文件末尾 | 0 |
| 空区间 | 0 |

所有实际正文请求包含 Range、If-Match 并关闭响应体；游标 SSE 每次最多读取 65,536 字节，完整重组 2,300,041 字节，与原始日志逐字节一致，收到 done。旧无 limit 的完整 GET 保持兼容。真实作业 succeeded、停止凭据 verified；两次实际 API 出生身份均确认退出，隔离 S3 已停止。

## 环境与中间失败

- 初次观察器替换了 Client 类型，触发底层串行请求校验；改为包装原方法返回的实际响应体。保留 `tmp/job-log-range-capacity-e2qa0p3q`。
- 本机系统代理截获 loopback 签名 S3 请求并返回 502，导致服务就绪超时。Windows/WSL UTC 时间一致，直接签名请求成功。仅在测试进程中设置 `NO_PROXY=127.0.0.1,localhost,::1`，结束后还原该进程环境；没有修改系统代理。超时证据包括 `tmp/job-log-range-capacity-8o1tgjn3`，独立诊断服务已停止。
- 脚本误用日志字段及路径，真实作业完成后读取返回 404；修正为公开返回值 `log_path` 和实际 `.vibeflow/job-logs/` 路径。失败及完整源码保留于 `tmp/job-log-range-capacity-8j832ehp`。
- 修复后的首次运行在作业准入前遇到实际 `storage_quota_busy` 409，未进入分页检查。保留 `tmp/job-log-range-capacity-nhn6dbbg` 及完整源码；准备阶段仅对此明确拒绝增加最多 30 秒的重试，记录每次实际拒绝，不重试已接受请求、不吞掉其他错误。生产分页实现没有因此改动。

真实模型调用 **0**、模拟模型调用 **0**、Claude 调用 **0**。这是实际命令与 S3 网络读取验收，不是模型验收；不宣称已测本次变更下的并发替换冲突。S3 元数据访问、同步 I/O 占用事件循环/数据库连接、旧 SSE 订阅清理及其他全局并发仍按原清单推进，未将局部修复计作整个 N4-05 完成。
